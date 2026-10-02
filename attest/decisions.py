"""The decision ledger - the piece that makes an arbiter measurable.

WHY THIS EXISTS
---------------
A decision model answers with a probability, and everyone repeats the number.
Measured on 108 claims in September 2026, the calibration advantage the coverage
described was not there: expected calibration error came out at 0.066 for Jev
against 0.061 and 0.067 for two cheap chat models. At that sample size those are
one number.

Which is the whole problem. A claimed confidence is not evidence of anything
until somebody has counted how often it was right - and the only person who can
count on YOUR decisions, with YOUR state and YOUR outcomes, is you.

So this records every decision with the confidence it was made at, and later
what actually happened. It then refuses to report a rate it has not earned the
right to report.

THREE RULES, and they are the same three rules as everywhere else here:

  1. AN UNRESOLVED DECISION IS NOT A CORRECT ONE. It is counted separately and
     excluded from every rate. Silence is not success.
  2. A RATE BELOW THE THRESHOLD IS NOT REPORTED AS A NUMBER. Below MIN_N the
     bucket says INSUFFICIENT DATA and gives you n. Two points of difference at
     n=108 is noise, and printing it as a decimal invites someone to act on it.
  3. OVERCONFIDENCE IS ONLY CLAIMED WHEN THE INTERVAL EXCLUDES THE CLAIM. Every
     bucket carries a Wilson interval, because a point estimate with no interval
     is the same lie as a confidence with no tally.
"""

from __future__ import annotations

import json
import math
import time
import uuid
from typing import Any, Dict, Optional, Tuple

# Below this many resolved decisions, no rate is reported at all. Chosen from
# the measurement above: 108 items could not separate 0.066 from 0.061.
MIN_N = 30

BUCKETS: Tuple[Tuple[float, float], ...] = (
    (0.00, 0.50), (0.50, 0.70), (0.70, 0.85), (0.85, 0.95), (0.95, 1.01),
)


def wilson(hits: int, n: int, z: float = 1.96) -> Tuple[float, float]:
    """Wilson score interval. A point estimate without one is not a measurement."""
    if n <= 0:
        return (0.0, 1.0)
    p = hits / n
    d = 1.0 + z * z / n
    centre = (p + z * z / (2 * n)) / d
    half = (z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n))) / d
    return (max(0.0, centre - half), min(1.0, centre + half))


def bucket_of(p: float) -> str:
    for lo, hi in BUCKETS:
        if lo <= p < hi:
            return "%.2f-%.2f" % (lo, hi)
    return "%.2f-%.2f" % BUCKETS[-1]


class DecisionLedger:
    """Records decisions and the outcomes that eventually settle them."""

    def __init__(self, store):
        self.store = store
        self.db = store.db

    # ------------------------------------------------------------------ record
    def record(self, question: str, answer: str, qtype: str = "choice",
               probability: Optional[float] = None,
               confidence: Optional[float] = None,
               state: str = "", model: str = "", who: str = "",
               alternatives: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        """Log one decision, with the confidence it was made at.

        The probability is REQUIRED for a noul question. A boolean judgment with
        no probability is exactly the unmeasured claim this ledger exists to
        catch, and it is refused rather than stored as if it were fine.
        """
        qtype = (qtype or "").strip().lower()
        if qtype not in ("choice", "noul", "score"):
            return {"ok": False, "error": "unknown qtype: %r" % qtype,
                    "accepted": ["choice", "noul", "score"]}
        if qtype == "noul" and probability is None:
            return {"ok": False,
                    "error": "a noul decision MUST carry its probability; a "
                             "judgment with no confidence cannot be scored later"}
        did = "dec:" + uuid.uuid4().hex[:12]
        self.db.execute(
            "INSERT INTO decisions (id, at, who, model, state, question, qtype,"
            " answer, probability, confidence, alternatives, outcome, correct,"
            " resolved_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,NULL,NULL,NULL)",
            (did, time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), who[:120],
             model[:120], state[:4000], question[:500], qtype, str(answer)[:300],
             (None if probability is None else float(probability)),
             (None if confidence is None else float(confidence)),
             json.dumps(alternatives or {})[:4000]))
        self.db.commit()
        self.store.ledger("decision_recorded", "%s %s" % (did, qtype))
        return {"ok": True, "id": did, "status": "UNRESOLVED",
                "note": "unresolved until an outcome is recorded; it is not "
                        "counted in any rate before then"}

    # ----------------------------------------------------------------- resolve
    def resolve(self, decision_id: str, outcome: str = "",
                correct: Optional[bool] = None) -> Dict[str, Any]:
        """Record what actually happened.

        Refuses to resolve the same decision twice. A decision whose answer can
        be quietly revised after the fact is not a prediction, it is a
        retrospective.
        """
        row = self.db.execute("SELECT outcome, correct FROM decisions WHERE id=?",
                              (decision_id,)).fetchone()
        if row is None:
            return {"ok": False, "error": "no such decision: %s" % decision_id}
        if row["outcome"] is not None or row["correct"] is not None:
            return {"ok": False,
                    "error": "already resolved - a decision may be settled once",
                    "current": {"outcome": row["outcome"], "correct": row["correct"]}}
        if correct is None:
            return {"ok": False,
                    "error": "correct must be true or false. 'unknown how it "
                             "turned out' is not an outcome; it stays unresolved."}
        self.db.execute(
            "UPDATE decisions SET outcome=?, correct=?, resolved_at=? WHERE id=?",
            (outcome[:500], 1 if correct else 0,
             time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), decision_id))
        self.db.commit()
        self.store.ledger("decision_resolved", "%s correct=%s" % (decision_id, correct))
        return {"ok": True, "id": decision_id, "correct": bool(correct)}

    # ------------------------------------------------------------- calibration
    def calibration(self, model: Optional[str] = None) -> Dict[str, Any]:
        """How often was a decision taken at confidence p actually right?

        Per bucket, with an interval, and it refuses to be a number when there
        is not enough evidence for one.
        """
        q = ("SELECT probability, confidence, correct FROM decisions "
             "WHERE correct IS NOT NULL")
        args: Tuple[Any, ...] = ()
        if model:
            q += " AND model = ?"
            args = (model,)
        rows = self.db.execute(q, args).fetchall()
        unresolved = int(self.db.execute(
            "SELECT COUNT(*) FROM decisions WHERE correct IS NULL").fetchone()[0])
        total = unresolved + len(rows)

        buckets: Dict[str, Dict[str, Any]] = {}
        for r in rows:
            p = r["probability"]
            if p is None:
                continue
            b = buckets.setdefault(bucket_of(float(p)),
                                   {"n": 0, "hits": 0, "claimed": []})
            b["n"] += 1
            b["hits"] += int(r["correct"])
            b["claimed"].append(float(p))

        out_buckets = []
        for lo, hi in BUCKETS:
            name = "%.2f-%.2f" % (lo, hi)
            b = buckets.get(name)
            if not b:
                continue
            n, hits = b["n"], b["hits"]
            claimed = sum(b["claimed"]) / len(b["claimed"])
            entry: Dict[str, Any] = {"bucket": name, "n": n, "hits": hits,
                                     "claimed_confidence": round(claimed, 4)}
            if n < MIN_N:
                entry["observed_rate"] = None
                entry["interval"] = None
                entry["verdict"] = "INSUFFICIENT DATA"
                entry["note"] = ("n=%d, below the threshold of %d. No rate is "
                                 "reported: at this sample size a few points of "
                                 "difference is noise." % (n, MIN_N))
            else:
                obs = hits / n
                low, high = wilson(hits, n)
                entry["observed_rate"] = round(obs, 4)
                entry["interval"] = [round(low, 4), round(high, 4)]
                if claimed > high:
                    entry["verdict"] = "OVERCONFIDENT"
                elif claimed < low:
                    entry["verdict"] = "UNDERCONFIDENT"
                else:
                    entry["verdict"] = "consistent with its claim"
            out_buckets.append(entry)

        resolved = len(rows)
        if resolved < MIN_N:
            overall = ("INSUFFICIENT DATA - %d of %d decisions resolved, %d needed. "
                       "No calibration claim can be made from this ledger yet."
                       % (resolved, total, MIN_N))
        else:
            hits = sum(r["correct"] for r in rows)
            overall = ("%d of %d resolved decisions were right (%.1f%%), across %d "
                       "recorded." % (hits, resolved, 100.0 * hits / resolved, total))
        if unresolved:
            overall += (" %d decision(s) are UNRESOLVED and are excluded from every "
                        "rate above - an unsettled decision is not a correct one."
                        % unresolved)
        return {"ok": True, "model": model or "(all)", "recorded": total,
                "resolved": resolved, "unresolved": unresolved, "min_n": MIN_N,
                "buckets": out_buckets, "verdict": overall}
