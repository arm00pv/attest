"""The memory. A fact is stored WITH the provenance of who said it and whether
anything checked it, and the two are never merged.

WHY SQLITE AND NOT A VECTOR STORE
---------------------------------
Because retrieval is not the hard part and it is not what this is for. Mem0,
Zep and Letta do similarity search well and are funded to keep doing it. What
none of them store is whether a fact was CHECKED, by what, and when - their
memories are all equally confident.

So recall() here is a token match over the text, deliberately and openly. It is
weaker at finding things than a vector index, and that weakness is stated in
the README rather than hidden. Bring your own embeddings if you want them; what
this adds is the tier.
"""

from __future__ import annotations

import os
import re
import sqlite3
import time
import uuid
from typing import Any, Dict, List, Optional

SCHEMA = """
CREATE TABLE IF NOT EXISTS facts (
    id          TEXT PRIMARY KEY,
    fact        TEXT NOT NULL,
    why         TEXT NOT NULL DEFAULT '',
    who         TEXT NOT NULL DEFAULT '',
    tier        TEXT NOT NULL CHECK (tier IN ('verified', 'asserted')),
    checker     TEXT NOT NULL DEFAULT '',
    domain      TEXT NOT NULL DEFAULT '',
    created_at  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS facts_tier ON facts(tier);
CREATE INDEX IF NOT EXISTS facts_created ON facts(created_at);
CREATE TABLE IF NOT EXISTS triples (
    source      TEXT NOT NULL,
    relation    TEXT NOT NULL,
    target      TEXT NOT NULL,
    tier        TEXT NOT NULL CHECK (tier IN ('verified', 'asserted')),
    who         TEXT NOT NULL DEFAULT '',
    why         TEXT NOT NULL DEFAULT '',
    created_at  TEXT NOT NULL,
    PRIMARY KEY (source, relation, target)
);
CREATE TABLE IF NOT EXISTS ledger (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    at          TEXT NOT NULL,
    event       TEXT NOT NULL,
    detail      TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS decisions (
    id           TEXT PRIMARY KEY,
    at           TEXT NOT NULL,
    who          TEXT NOT NULL DEFAULT '',
    model        TEXT NOT NULL DEFAULT '',
    state        TEXT NOT NULL DEFAULT '',
    question     TEXT NOT NULL DEFAULT '',
    qtype        TEXT NOT NULL CHECK (qtype IN ('choice', 'noul', 'score')),
    answer       TEXT NOT NULL DEFAULT '',
    probability  REAL,
    confidence   REAL,
    alternatives TEXT NOT NULL DEFAULT '{}',
    outcome      TEXT,
    correct      INTEGER CHECK (correct IN (0, 1) OR correct IS NULL),
    resolved_at  TEXT,
    due_at       TEXT,
    stratum      TEXT NOT NULL DEFAULT '',
    cohort       TEXT NOT NULL DEFAULT '',
    target       TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS decisions_resolved ON decisions(correct);
CREATE INDEX IF NOT EXISTS decisions_due ON decisions(due_at);
"""

TIER_VERIFIED = "verified"
TIER_ASSERTED = "asserted"

# Columns added when the ledger learned to hold a decision OPEN. A decision made
# now and settled later is a different object from one that was scored in the
# same breath as it was recorded, and the two must be separable in a query.
#
# CREATE TABLE IF NOT EXISTS does nothing to a table that already exists, so an
# existing ledger needs the columns added explicitly. SQLite has no
# ADD COLUMN IF NOT EXISTS, so this checks PRAGMA first and is safe to re-run.
DECISION_COLUMNS = (
    ("due_at", "TEXT"),
    ("stratum", "TEXT NOT NULL DEFAULT ''"),
    ("cohort", "TEXT NOT NULL DEFAULT ''"),
    ("target", "TEXT NOT NULL DEFAULT ''"),
)


def _migrate(db) -> None:
    have = {r["name"] for r in db.execute("PRAGMA table_info(decisions)").fetchall()}
    for name, decl in DECISION_COLUMNS:
        if name not in have:
            db.execute("ALTER TABLE decisions ADD COLUMN %s %s" % (name, decl))
    db.execute("CREATE INDEX IF NOT EXISTS decisions_due ON decisions(due_at)")
    db.commit()
WORD = re.compile(r"[a-z0-9_]{2,}")


def default_db_path() -> str:
    env = os.environ.get("ATTEST_DB")
    if env:
        return env
    home = os.environ.get("ATTEST_HOME") or os.path.join(
        os.path.expanduser("~"), ".attest")
    return os.path.join(home, "attest.db")


class Store:
    def __init__(self, path: Optional[str] = None):
        self.path = path or default_db_path()
        parent = os.path.dirname(os.path.abspath(self.path))
        if parent:
            os.makedirs(parent, exist_ok=True)
        fresh = not os.path.exists(self.path)
        self.db = sqlite3.connect(self.path, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.executescript(SCHEMA)
        self.db.commit()
        _migrate(self.db)
        if fresh:
            self.ledger("store_created", self.path)

    # ------------------------------------------------------------------ ledger
    def ledger(self, event: str, detail: str = "") -> None:
        try:
            self.db.execute("INSERT INTO ledger (at, event, detail) VALUES (?,?,?)",
                            (time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                             event, detail[:400]))
            self.db.commit()
        except Exception:
            pass  # the ledger must never be able to fail the operation it records

    def ledger_count(self) -> int:
        try:
            return int(self.db.execute("SELECT COUNT(*) FROM ledger").fetchone()[0])
        except Exception:
            return 0

    # ---------------------------------------------------------------- remember
    def remember(self, fact: str, why: str = "", who: str = "",
                 verified: bool = False, checker: str = "",
                 domain: str = "") -> Dict[str, Any]:
        """Store one finding, tagged by tier.

        verified=True is a claim that a checker PASSED this. If that is not true,
        pass False - an asserted fact presented as verified defeats the only
        thing this store is for.

        Note what is NOT here: a way to store something with no tier at all.
        The schema CHECK constraint makes the untagged row impossible.
        """
        tier = TIER_VERIFIED if verified else TIER_ASSERTED
        fid = "fact:" + uuid.uuid4().hex[:12]
        self.db.execute(
            "INSERT INTO facts (id, fact, why, who, tier, checker, domain, created_at)"
            " VALUES (?,?,?,?,?,?,?,?)",
            (fid, fact[:2000], why[:2000], who[:120], tier, checker[:120],
             domain[:120], time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())))
        self.db.commit()
        self.ledger("remember", "%s tier=%s who=%s" % (fid, tier, who))
        return {"ok": True, "id": fid, "tier": tier, "who": who,
                "created_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}

    # ------------------------------------------------------------------ recall
    def recall(self, query: str, limit: int = 5,
               only_verified: bool = False) -> Dict[str, Any]:
        """Read findings back. The tier is preserved on every row.

        A caller can therefore tell, without trusting this service's word, which
        of the things it just read were checked and which were merely asserted.
        """
        terms = set(WORD.findall(query.lower()))
        if only_verified:
            rows = self.db.execute(
                "SELECT * FROM facts WHERE tier = 'verified'").fetchall()
        else:
            rows = self.db.execute("SELECT * FROM facts").fetchall()

        scored = []
        for r in rows:
            hay = (r["fact"] + " " + r["why"] + " " + r["domain"]).lower()
            hits = sum(1 for t in terms if t in hay)
            if terms and hits == 0:
                continue
            scored.append((hits, r["created_at"], r))
        # Verified first, then more matching terms, then more recent. A checked
        # fact outranks an asserted one even when the asserted one matches better.
        # Two stable sorts, because created_at is a string and cannot be negated.
        scored.sort(key=lambda x: x[2]["created_at"], reverse=True)
        scored.sort(key=lambda x: (0 if x[2]["tier"] == TIER_VERIFIED else 1,
                                   -x[0]))
        results = []
        for hits, _ts, r in scored[:max(1, limit)]:
            results.append({
                "id": r["id"], "fact": r["fact"], "why": r["why"],
                "who": r["who"], "tier": r["tier"], "checker": r["checker"],
                "domain": r["domain"], "created_at": r["created_at"],
                "verified": r["tier"] == TIER_VERIFIED,
                "matched_terms": hits,
            })
        return {
            "ok": True, "query": query, "count": len(results),
            "verified_total": sum(1 for r in results if r["verified"]),
            "asserted_total": sum(1 for r in results if not r["verified"]),
            "note": ("tier is 'verified' only where a checker passed the fact; "
                     "'asserted' means an agent said it and nothing checked it"),
            "results": results,
        }

    def counts(self) -> Dict[str, int]:
        out = {}
        for tier in (TIER_VERIFIED, TIER_ASSERTED):
            out[tier] = int(self.db.execute(
                "SELECT COUNT(*) FROM facts WHERE tier = ?", (tier,)).fetchone()[0])
        out["total"] = out[TIER_VERIFIED] + out[TIER_ASSERTED]
        return out
