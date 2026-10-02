#!/usr/bin/env python3
"""Conformance controls for attest.

Every control here exists because something was once reported wrongly, or
because a detector that cannot fire is worse than no detector at all.

A control that cannot run reports SKIPPED, never PASS. A skipped control and a
passing control are different facts, and collapsing them is how a suite becomes
decoration.

    python3 tests/test_conformance.py
"""

from __future__ import annotations

import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

from attest import checkers, normalize, Service, Verdict          # noqa: E402
from attest.store import Store, TIER_ASSERTED, TIER_VERIFIED      # noqa: E402

PASS, FAIL, SKIP = [], [], []


def ok(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print("  %-5s %s%s" % ("PASS" if cond else "FAIL", name,
                           "" if cond else "   " + str(detail)[:220]))


def skip(name, why):
    SKIP.append(name)
    print("  %-5s %s   (%s)" % ("SKIP", name, why))


def tmp_service():
    d = tempfile.mkdtemp(prefix="attest_test_")
    return Service(os.path.join(d, "t.db")), d


# ---------------------------------------------------------------- the rule

def c001_a_bare_bool_cannot_carry_unknown():
    """A bool has two states and a verdict has three. Anything that cannot say
    'I could not tell' will eventually say 'false' instead."""
    v = normalize(False)
    ok("C001 a bare bool normalises to a decided verdict", v.outcome == "disputed",
       v.outcome)
    v = normalize({})
    ok("C001b a dict with NO verdict key normalises to UNKNOWN",
       v.outcome == "unknown" and v.checker_error, v.outcome)
    v = normalize(None)
    ok("C001c None normalises to UNKNOWN, never to a pass",
       v.outcome == "unknown", v.outcome)


def c002_anti_blindness_timeout_is_unknown():
    """ANTI-BLINDNESS. If this cannot fire, nothing downstream means anything:
    a checker that never answered would be recorded as a disproof."""
    v = checkers.check_code("import time\ntime.sleep(30)", timeout=2)
    ok("C002 a checker that timed out reports UNKNOWN", v.outcome == "unknown",
       "%s / %s" % (v.outcome, v.reason))
    ok("C002b and it is NOT reported as a disproof", not v.decided, v.to_dict())


def c003_a_real_failure_is_disputed():
    """The negative control for C002. If everything came back UNKNOWN the suite
    would pass C002 while being useless."""
    v = checkers.check_code("raise SystemExit(3)")
    ok("C003 a claim that genuinely fails reports DISPUTED",
       v.outcome == "disputed", "%s / %s" % (v.outcome, v.reason))
    v2 = checkers.check_code("print(2 + 2)")
    ok("C003b and a claim that genuinely holds reports VERIFIED",
       v2.outcome == "verified", v2.to_dict())


def c004_signal_death_is_unknown():
    """Measured on the machine this came from: failing RAM made Lean die with
    SIGSEGV and print nothing, which is byte for byte what a false claim
    produces. Every crashed check was being recorded as a disproof."""
    if os.name != "posix":
        return skip("C004 a checker killed by a signal reports UNKNOWN",
                    "no POSIX signals on this platform")
    v = checkers.check_code("import os, signal\nos.kill(os.getpid(), signal.SIGKILL)")
    ok("C004 a checker killed by a signal reports UNKNOWN",
       v.outcome == "unknown", "%s / %s" % (v.outcome, v.reason))
    ok("C004b and it names the signal", "signal" in json.dumps(v.detail),
       v.detail)


def c005_no_path_to_verified_without_a_checker():
    """A structural control, not a behavioural one: outside verdict.py, which
    owns the three constructors, no call in this codebase may set
    verified=True.

    Implemented over the AST rather than by scanning text. The first version
    scanned lines and failed on its own docstrings, because a comment stripper
    does not know what a docstring is - the control was measuring my grepping,
    not the code."""
    import ast
    offenders = []
    for mod in ("checkers.py", "service.py", "store.py", "http_api.py",
                "mcp_api.py", "__main__.py"):
        p = os.path.join(ROOT, "attest", mod)
        if not os.path.exists(p):
            continue
        with open(p, encoding="utf-8") as fh:
            tree = ast.parse(fh.read(), filename=p)
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            for kw in node.keywords:
                if (kw.arg == "verified"
                        and isinstance(kw.value, ast.Constant)
                        and kw.value.value is True):
                    offenders.append("%s:%d" % (mod, node.lineno))
    ok("C005 no module outside verdict.py constructs verified=True",
       not offenders, "found in: " + ", ".join(offenders))
    # And the control must be able to fire at all.
    src = "Verdict(verified=True)"
    planted = [n for n in ast.walk(ast.parse(src)) if isinstance(n, ast.Call)
               for kw in n.keywords
               if kw.arg == "verified" and isinstance(kw.value, ast.Constant)
               and kw.value.value is True]
    ok("C005b and the AST check actually detects a planted violation",
       len(planted) == 1, "the detector is blind")


def c006_the_auditor_can_actually_complain():
    """A control on the control. never_defaults_true() must fail on a bad
    verdict, or it is a function that always returns None and looks like
    diligence."""
    bad = Verdict(verified=True, checker="")
    ok("C006 the auditor complains about a pass that names no checker",
       checkers and normalize and __import__("attest").verdict
       .never_defaults_true([bad]) is not None)
    good = Verdict.passed("code", seconds=0.1)
    ok("C006b and stays silent on a well-formed pass",
       __import__("attest").verdict.never_defaults_true([good]) is None)


# ---------------------------------------------------------------- the memory

def c007_only_verified_excludes_asserted():
    svc, d = tmp_service()
    svc.remember("the reactor is at 300 K", who="a", verified=False)
    svc.remember("the reactor is at 310 K", who="b", verified=True, checker="probe")
    out = svc.recall("reactor", only_verified=True)
    facts = [r["fact"] for r in out["results"]]
    ok("C007 only_verified returns ONLY checked facts",
       len(facts) == 1 and "310" in facts[0], facts)
    ok("C007b and the counts say so", out["asserted_total"] == 0, out["counts"]
       if "counts" in out else out.get("asserted_total"))


def c008_tier_survives_the_round_trip():
    svc, d = tmp_service()
    svc.remember("a fact nobody checked", who="agent", verified=False)
    out = svc.recall("nobody checked")
    r = out["results"][0]
    ok("C008 an asserted fact comes back verified=false",
       r["verified"] is False and r["tier"] == TIER_ASSERTED, r)
    svc.remember("a fact a checker passed", who="probe", verified=True,
                 checker="code")
    out2 = svc.recall("checker passed")
    r2 = out2["results"][0]
    ok("C008b a verified fact comes back verified=true with its checker named",
       r2["verified"] is True and r2["tier"] == TIER_VERIFIED
       and r2["checker"] == "code", r2)


def c009_untiered_facts_are_impossible():
    """The schema, not the code, is what makes this unrepresentable."""
    svc, d = tmp_service()
    try:
        svc.store.db.execute(
            "INSERT INTO facts (id, fact, tier, created_at) VALUES (?,?,?,?)",
            ("x", "a fact with no tier", "maybe", "now"))
        svc.store.db.commit()
        ok("C009 the store refuses a fact with an invented tier", False,
           "the CHECK constraint did not fire")
    except sqlite3.IntegrityError:
        ok("C009 the store refuses a fact with an invented tier", True)
    except Exception as exc:
        ok("C009 the store refuses a fact with an invented tier", False, exc)


def c010_verified_outranks_asserted():
    """A checked fact must beat an asserted one that matches better, or the
    ranking quietly undoes the point of the tier."""
    svc, d = tmp_service()
    svc.remember("widget widget widget temperature", who="agent", verified=False)
    svc.remember("widget temperature", who="probe", verified=True, checker="probe")
    out = svc.recall("widget temperature")
    ok("C010 the verified fact ranks above the better-matching asserted one",
       out["results"][0]["verified"] is True,
       [(r["verified"], r["matched_terms"]) for r in out["results"]])


# ---------------------------------------------------------------- the checkers

def c011_kind_aliases_and_refusal():
    ok("C011 'lean' resolves to the math checker",
       checkers.resolve_kind("lean", "x") == "math")
    ok("C011b 'graph' resolves to the graph checker",
       checkers.resolve_kind("graph", "x") == "graph")
    ok("C011c an invented kind resolves to None rather than a guess",
       checkers.resolve_kind("tarot", "x") is None)


def c012_unknown_kind_fails_rather_than_defaulting():
    svc, d = tmp_service()
    out = svc.verify("something", kind="tarot")
    ok("C012 verify refuses an unknown kind instead of picking a checker",
       out.get("ok") is False and "accepted" in out, out)


def c013_absent_lean_is_unknown_not_disputed():
    if shutil.which("lean") or shutil.which("lake"):
        return skip("C013 a claim checked with no Lean reports UNKNOWN",
                    "Lean IS installed here, so the absence cannot be tested")
    v = checkers.check_math("theorem t : 1 + 1 = 2 := by decide")
    ok("C013 with no Lean, a math claim reports UNKNOWN", v.outcome == "unknown",
       v.outcome)
    # Case-insensitive, deliberately. The first version looked for "cannot be
    # checked" and the reason reads "CANNOT be checked" - the same defect as an
    # assertion that looked for "recreate" inside "recreating".
    ok("C013b and says math cannot be checked here, rather than that the claim "
       "is false", "cannot be checked" in v.reason.lower(), v.reason)
    ok("C013c and it offers a remedy rather than a dead end",
       "remedy" in v.detail, v.detail)


def c014_capabilities_is_measured_not_asserted():
    svc, d = tmp_service()
    svc.warm()
    caps = svc.capabilities()
    bad = [k for k in ("math", "code", "graph")
           if "ok" not in caps["checks"].get(k, {})]
    ok("C014 every checker reports a measured ok flag", not bad, bad)
    un = [k for k in ("math", "code", "graph")
          if not caps["checks"][k].get("ok")]
    if un:
        ok("C014b the top-level verdict NAMES the checkers that cannot run",
           all(u in caps["verdict"] for u in un), caps["verdict"])
    else:
        ok("C014b every checker measured ok, so there is nothing to name",
           "can check:" in caps["verdict"], caps["verdict"])
    ok("C014c and it reports its own staleness rather than hiding it",
       "cache_age_seconds" in caps and "stale" in caps, list(caps)[:8])


def c015_graph_absence_is_unknown_and_contradiction_is_disputed():
    svc, d = tmp_service()
    v = svc.graph.check("nobody", "KNOWS", "this")
    ok("C015 a triple the graph has never heard of is UNKNOWN, not disproved",
       v.outcome == "unknown", v.outcome)
    svc.graph.add("earth", "ORBITS", "sun", tier="verified")
    v2 = svc.graph.check("earth", "ORBITS", "sun")
    ok("C015b a triple that IS present verifies", v2.outcome == "verified",
       v2.outcome)
    svc.graph.add("earth", "ORBITS", "mars", tier="asserted")
    v3 = svc.graph.check("earth", "ORBITS", "jupiter")
    ok("C015c the same source and relation pointing elsewhere is DISPUTED",
       v3.outcome == "disputed", "%s / %s" % (v3.outcome, v3.reason))


def c016_memcheck_never_claims_health_from_silence():
    svc, d = tmp_service()
    out = svc.memcheck(gb=1, passes=1)
    ok("C016 memcheck runs and returns a verdict", out.get("ok") is True, out)
    if out.get("reproducible_count") == 0:
        ok("C016b finding nothing does NOT get reported as the memory being good",
           "does NOT prove" in out.get("verdict", "")
           or "does not prove" in out.get("verdict", ""), out.get("verdict"))
    else:
        ok("C016b a real fault is reported as failing memory",
           "FAILING MEMORY" in out.get("verdict", ""), out.get("verdict"))


def c017_warm_actually_waits():
    """capabilities() is asynchronous by design - measuring it runs real probes.
    A synchronous caller that asks for it must get a RESULT, not the warming
    state, or 'I have not looked' gets printed as 'I cannot see'."""
    svc, d = tmp_service()
    first = svc.capabilities()
    ok("C017 a cold capabilities() honestly reports that it is warming",
       first.get("warming") is True and first.get("measured") is False, first)
    ok("C017b and does NOT render that as a failure state",
       "NOT YET MEASURED" in first.get("verdict", ""), first.get("verdict"))
    got = svc.warm(timeout=120)
    ok("C017c warm() blocks until there is a measurement", got is True, got)
    second = svc.capabilities()
    ok("C017d and the next call is a real measured result",
       second.get("measured") is True and second.get("ok") is True,
       list(second)[:8])
    ok("C017e with a verdict that names what it can check",
       "can check:" in second.get("verdict", ""), second.get("verdict"))


def c018_missing_dependency_is_not_a_disproof():
    """Measured on throne 2026-10-01: a TRUE theorem came back DISPUTED because
    Mathlib was not installed, so 'norm_num' did not exist. The checker ran and
    the rejection had nothing to do with the claim - the exact failure this
    service exists to prevent, found in this service by running it."""
    if not (shutil.which("lean") or shutil.which("lake")):
        return skip("C018 a missing dependency is UNKNOWN, not DISPUTED",
                    "no Lean here at all, so the case cannot arise")
    if checkers.mathlib_dir():
        return skip("C018 a missing dependency is UNKNOWN, not DISPUTED",
                    "Mathlib IS configured here, so its absence cannot be tested")
    v = checkers.check_math("theorem t : (1 : Nat) + 1 = 2 := by norm_num")
    ok("C018 with no Mathlib, a Mathlib-dependent claim is UNKNOWN",
       v.outcome == "unknown", "%s / %s" % (v.outcome, v.reason))
    ok("C018b and it names the missing dependency and a remedy rather than "
       "saying the theorem is false",
       "MISSING DEPENDENCY" in v.reason and "remedy" in v.detail, v.detail)


def c019_a_judgment_with_no_confidence_is_refused():
    """A boolean with no probability attached is the unmeasured claim this
    ledger exists to catch, so it must not be storable."""
    svc, d = tmp_service()
    r = svc.record_decision("is the door locked?", "yes", qtype="noul")
    ok("C019 a noul decision with no probability is REFUSED",
       r.get("ok") is False and "probability" in r.get("error", ""), r)
    r2 = svc.record_decision("is the door locked?", "yes", qtype="noul",
                             probability=0.8)
    ok("C019b with a probability it is accepted", r2.get("ok") is True, r2)


def c020_an_unresolved_decision_is_not_a_correct_one():
    """ANTI-BLINDNESS for the ledger. A decision nobody ever settled must not
    inflate a hit rate. It is the same lie as a checker that never ran."""
    svc, d = tmp_service()
    for _ in range(40):
        svc.record_decision("q", "a", qtype="noul", probability=0.9)
    one = svc.record_decision("q", "a", qtype="noul", probability=0.9)["id"]
    svc.resolve_decision(one, "it turned out right", True)
    cal = svc.calibration()
    ok("C020 the ledger reports 40 unresolved and 1 resolved, not 41 correct",
       cal["unresolved"] == 40 and cal["resolved"] == 1,
       {"unresolved": cal["unresolved"], "resolved": cal["resolved"]})
    ok("C020b and claims no rate from a single observation",
       "INSUFFICIENT DATA" in cal["verdict"], cal["verdict"])


def c021_a_decision_is_settled_once():
    svc, d = tmp_service()
    did = svc.record_decision("q", "a", qtype="noul", probability=0.5)["id"]
    ok("C021 the first resolution is accepted",
       svc.resolve_decision(did, "yes", True).get("ok") is True)
    r = svc.resolve_decision(did, "actually no", False)
    ok("C021b a second resolution is REFUSED - a prediction may not be revised "
       "after the fact", r.get("ok") is False, r)


def c022_an_unknown_outcome_stays_unresolved():
    svc, d = tmp_service()
    did = svc.record_decision("q", "a", qtype="noul", probability=0.5)["id"]
    r = svc.resolve_decision(did, "nobody ever checked")
    ok("C022 resolving without saying whether it was right is refused",
       r.get("ok") is False, r)
    ok("C022b so it stays unresolved rather than being counted either way",
       svc.calibration()["unresolved"] == 1, svc.calibration())


def c023_overconfidence_is_detected_and_only_then():
    """The negative control for the ledger. If this cannot fire, the whole
    apparatus is decoration."""
    svc, d = tmp_service()
    for i in range(40):
        did = svc.record_decision("q", "a", qtype="noul", probability=0.95)["id"]
        svc.resolve_decision(did, "outcome", i % 2 == 0)   # right ~50% at 0.95
    cal = svc.calibration()
    b = [x for x in cal["buckets"] if x["bucket"].startswith("0.95")]
    ok("C023 a bucket claiming 0.95 and right half the time is OVERCONFIDENT",
       bool(b) and b[0]["verdict"] == "OVERCONFIDENT", b)
    ok("C023b and it reports the interval rather than a bare point estimate",
       bool(b) and b[0]["interval"] is not None
       and b[0]["interval"][0] < b[0]["observed_rate"], b)


def c024_a_well_calibrated_bucket_is_not_called_overconfident():
    """The other half of C023. A detector that calls everything overconfident is
    as useless as one that calls nothing."""
    svc, d = tmp_service()
    for i in range(100):
        did = svc.record_decision("q", "a", qtype="noul", probability=0.8)["id"]
        svc.resolve_decision(did, "outcome", i % 10 < 8)   # exactly 80%
    cal = svc.calibration()
    b = [x for x in cal["buckets"] if x["bucket"].startswith("0.70")]
    ok("C024 a bucket claiming 0.80 and right 80% of the time is consistent",
       bool(b) and b[0]["verdict"] == "consistent with its claim", b)


def c025_wilson_brackets_the_estimate():
    from attest.decisions import wilson
    lo, hi = wilson(50, 100)
    ok("C025 the interval brackets the point estimate", lo < 0.5 < hi, (lo, hi))
    lo2, hi2 = wilson(5, 10)
    ok("C025b and it is WIDER on fewer samples - the entire reason it is here",
       (hi2 - lo2) > (hi - lo), ((lo, hi), (lo2, hi2)))


# ------------------------------------------------------ the prospective half
#
# Everything above settles a decision in the same breath as it records one. That
# is a retrospective, and a ledger full of retrospectives can report a flawless
# calibration while never once having been wrong about the future, because it
# was never about the future. These control the half that is.


def c037_an_existing_ledger_gains_the_prospective_columns():
    """The bug this was written to catch, found on the real estate ledger.

    The index on due_at was created by the schema script, which runs BEFORE the
    migration that adds the column. On a database that already existed, CREATE
    TABLE IF NOT EXISTS did nothing, so due_at was still missing when the index
    was built and the store refused to open at all. A brand-new database takes a
    different path and worked perfectly, which is exactly why it got through:
    the fresh path and the upgrade path are not the same path, and only one of
    them had ever been exercised."""
    from attest.decisions import now_iso
    from attest.decisions import DecisionLedger
    d = tempfile.mkdtemp(prefix="attest_mig_")
    p = os.path.join(d, "old.db")
    con = sqlite3.connect(p)
    con.executescript("""
CREATE TABLE decisions (
    id TEXT PRIMARY KEY, at TEXT NOT NULL, who TEXT NOT NULL DEFAULT '',
    model TEXT NOT NULL DEFAULT '', state TEXT NOT NULL DEFAULT '',
    question TEXT NOT NULL DEFAULT '',
    qtype TEXT NOT NULL CHECK (qtype IN ('choice','noul','score')),
    answer TEXT NOT NULL DEFAULT '', probability REAL, confidence REAL,
    alternatives TEXT NOT NULL DEFAULT '{}', outcome TEXT,
    correct INTEGER CHECK (correct IN (0,1) OR correct IS NULL), resolved_at TEXT);
CREATE INDEX decisions_resolved ON decisions(correct);
""")
    con.execute("INSERT INTO decisions (id, at, qtype) VALUES "
                "('dec:old','2026-01-01T00:00:00Z','noul')")
    con.commit()
    con.close()

    try:
        s = Store(p)
        opened = True
    except Exception as exc:
        ok("C037 a ledger written before the prospective columns existed still "
           "opens", False, "%s: %s" % (type(exc).__name__, exc))
        return
    ok("C037 a ledger written before the prospective columns existed still opens",
       opened)
    cols = {r["name"] for r in s.db.execute("PRAGMA table_info(decisions)")}
    ok("C037b and it gains due_at, stratum, cohort and target",
       {"due_at", "stratum", "cohort", "target"} <= cols, sorted(cols))
    kept = s.db.execute("SELECT id FROM decisions").fetchall()
    ok("C037c with the decisions already in it left alone", len(kept) == 1, len(kept))
    r = DecisionLedger(s).record("q", "true", qtype="noul", probability=0.5,
                                 due_at=now_iso(600))
    ok("C037d and it can record a forecast afterwards", r.get("ok") is True, r)


def c026_a_forecast_must_be_about_the_future():
    from attest.decisions import now_iso
    svc, d = tmp_service()
    r = svc.record_decision("will t1 fail?", "true", qtype="noul", probability=0.7,
                            due_at=now_iso(3600), stratum="1h", cohort="c1",
                            target="t1")
    ok("C026 a decision due in the future is accepted and marked prospective",
       r.get("ok") is True and r.get("prospective") is True, r)
    r2 = svc.record_decision("will t1 fail?", "true", qtype="noul", probability=0.7,
                             due_at=now_iso(-3600))
    ok("C026b one whose outcome is ALREADY knowable is REFUSED - that is a "
       "retrospective wearing a forecast's name",
       r2.get("ok") is False and "retrospective" in r2.get("error", ""), r2)
    r3 = svc.record_decision("q", "a", qtype="noul", probability=0.5,
                             due_at="tomorrow")
    ok("C026c and a malformed due_at is refused rather than guessed at",
       r3.get("ok") is False, r3)


def c027_only_forecasts_come_due():
    from attest.decisions import now_iso
    svc, d = tmp_service()
    ledger = svc.decisions
    ledger.record("q", "true", qtype="noul", probability=0.5, target="never")
    ledger.record("q", "true", qtype="noul", probability=0.5,
                  due_at=now_iso(3600), target="later")
    ledger.record("q", "true", qtype="noul", probability=0.5,
                  due_at=now_iso(600), target="soon")
    # Query at +20 minutes: the +10m forecast has come due, the +60m one has not.
    due = ledger.due(now=now_iso(1200))
    ok("C027 only the forecast whose time has come is due", len(due) == 1,
       [d["target"] for d in due])
    ok("C027b the never-resolved retrospective is NOT swept up - 'nobody ever "
       "checked' must not quietly become a rate",
       all(x["due_at"] for x in due) and len(ledger.pending()) == 3,
       {"due": len(due), "pending": len(ledger.pending())})


def c028_an_unresolvable_forecast_stays_open():
    from attest.decisions import now_iso
    from attest.forecast import resolve_due
    svc, d = tmp_service()
    ledger = svc.decisions
    a = ledger.record("q", "true", qtype="noul", probability=0.5,
                      due_at=now_iso(60))["id"]
    ledger.record("q", "true", qtype="noul", probability=0.5, due_at=now_iso(61))

    def settle(dec):
        if dec["id"] == a:
            return None
        raise RuntimeError("the probe could not read the host")

    rep = resolve_due(ledger, settle, now=now_iso(7200))
    ok("C028 a resolver that cannot tell leaves the decision OPEN",
       rep["settled"] == 0, rep)
    ok("C028b and one that CRASHES is a failed measurement, not an outcome",
       any("raised" in x["why"] for x in rep["still_open"]), rep["still_open"])
    ok("C028c so the ledger still reports both as unresolved rather than "
       "inventing two answers",
       ledger.calibration()["unresolved"] == 2, ledger.calibration()["unresolved"])


def c029_the_ledger_can_still_catch_a_bad_forecaster():
    """THE CONTROL FOR THIS WHOLE LAYER. If a forecaster that claims 0.99 and is
    right half the time is not called overconfident, nothing built on top of it
    means anything, and the negative control here has to be able to fire."""
    from attest.decisions import now_iso
    from attest.forecast import Constant, Item, record_forecasts, resolve_due
    svc, d = tmp_service()
    ledger = svc.decisions
    items = [Item("t%d" % i, "will t fail?", stratum="ctl") for i in range(40)]
    rec = record_forecasts(ledger, items, [Constant(0.99)], cohort="ctl1",
                           due_at=now_iso(60))
    ok("C029 the control forecaster recorded 40 open decisions",
       rec["recorded"] == 40, rec)
    truth = {"t%d" % i: (i % 2 == 0) for i in range(40)}
    rep = resolve_due(ledger,
                      lambda dec: (str(truth[dec["target"]]), truth[dec["target"]]),
                      now=now_iso(7200))
    ok("C029b all forty settled against what actually happened",
       rep["settled"] == 40, rep["settled"])
    cal = ledger.calibration(model="constant-0.99", stratum="ctl")
    b = [x for x in cal["buckets"] if x["bucket"].startswith("0.95")]
    ok("C029c a forecaster claiming 0.99 and right half the time is OVERCONFIDENT",
       bool(b) and b[0]["verdict"] == "OVERCONFIDENT", b)


def c030_a_population_that_cannot_show_skill_is_named_as_such():
    svc, d = tmp_service()
    ledger = svc.decisions
    for _ in range(40):
        did = ledger.record("q", "true", qtype="noul", probability=0.9,
                            stratum="flat")["id"]
        ledger.resolve(did, "always the same", True)
    dis = ledger.discriminability(stratum="flat")
    ok("C030 forty identical outcomes are reported DEGENERATE, not as 100% skill",
       dis["can_demonstrate_skill"] is False and "DEGENERATE" in dis["verdict"], dis)
    for i in range(40):
        did = ledger.record("q", "true", qtype="noul", probability=0.5,
                            stratum="mixed")["id"]
        ledger.resolve(did, "varies", i % 2 == 0)
    dis2 = ledger.discriminability(stratum="mixed")
    ok("C030b while a genuinely mixed population is reported DISCRIMINATING - "
       "the detector is not simply always on",
       dis2["can_demonstrate_skill"] is True and "DISCRIMINATING" in dis2["verdict"],
       dis2)


def c031_a_worse_than_base_rate_forecaster_scores_negative():
    """Calibration is not skill. An always-0.30 forecaster is perfectly calibrated
    on a 30% population and carries no information at all, so a second score is
    needed that can rank them."""
    from attest.forecast import brier, brier_skill_score
    pairs = [(0.99, i % 2) for i in range(40)]          # claims 0.99, right 50%
    ref = sum((0.5 - y) ** 2 for _p, y in pairs) / len(pairs)
    s = brier_skill_score(pairs, ref)
    ok("C031 a forecaster worse than the base rate gets a NEGATIVE skill score",
       s is not None and s < 0, {"brier": brier(pairs), "ref": ref, "skill": s})
    good = [(0.97 if i % 2 else 0.03, i % 2) for i in range(40)]
    s2 = brier_skill_score(good, ref)
    ok("C031b and a genuinely good one scores positive - the score is not merely "
       "always negative", s2 is not None and s2 > 0, s2)


def c032_ranking_refuses_when_the_population_cannot_support_it():
    from attest.decisions import now_iso
    from attest.forecast import (Constant, Item, record_forecasts, resolve_due,
                                  head_to_head)
    svc, d = tmp_service()
    ledger = svc.decisions
    items = [Item("t%d" % i, "q?", stratum="h") for i in range(40)]
    record_forecasts(ledger, items, [Constant(0.9, "good"), Constant(0.5, "meh")],
                     cohort="h1", due_at=now_iso(60))
    resolve_due(ledger, lambda dec: ("yes", True), now=now_iso(7200))
    h = head_to_head(ledger, ["good", "meh"], stratum="h")
    ok("C032 with every outcome identical the ranking declines to rank",
       "cannot demonstrate skill" in h["verdict"], h["verdict"])
    ok("C032b and it says so per forecaster, not only in prose",
       all(r["can_demonstrate_skill"] is False for r in h["forecasters"]),
       h["forecasters"])


def c033_many_samples_in_one_cohort_are_not_many_observations():
    svc, d = tmp_service()
    ledger = svc.decisions
    for i in range(40):
        did = ledger.record("q", "true", qtype="noul", probability=0.9,
                            stratum="c", cohort="one-batch",
                            target="t%d" % i)["id"]
        ledger.resolve(did, "x", i % 2 == 0)
    cal = ledger.calibration(stratum="c")
    ok("C033 forty decisions inside one cohort are flagged as an OPTIMISTIC "
       "interval", cal["cohorts"] == 1 and cal["independence_warning"] is not None,
       cal.get("independence_warning"))
    ok("C033b and cohorts are counted next to decisions so the difference is visible",
       cal["per_cohort"] == 40.0, cal["per_cohort"])


def c034_a_forecast_nobody_settled_is_a_broken_resolver():
    from attest.decisions import now_iso
    svc, d = tmp_service()
    ledger = svc.decisions
    ledger.record("q", "true", qtype="noul", probability=0.5)                  # never a forecast
    ledger.record("q", "true", qtype="noul", probability=0.5,
                  due_at=now_iso(3600))                                        # simply early
    ledger.record("q", "true", qtype="noul", probability=0.5, due_at=now_iso(1))
    time.sleep(1.4)
    from attest.decisions import OVERDUE_GRACE_S
    open_ = ledger.calibration()["open"]
    ok("C034 the ledger separates 'not knowable yet' from 'just come due' from "
       "'was never a forecast'",
       open_["awaiting_outcome"] == 1 and open_["due_just_now"] == 1
       and open_["never_a_forecast"] == 1, open_)
    past = ledger.calibration(now=now_iso(OVERDUE_GRACE_S + 120))["open"]
    # Winding the clock forward also moves the other decision into the just-due
    # window, which is correct. The claim being tested is narrower: the SAME
    # decision was not a fault at 07:04 and is a fault an hour later.
    ok("C034b and the same decision becomes a broken resolver an hour later, "
       "having not been one before",
       open_["due_but_unrecorded"] == 0 and past["due_but_unrecorded"] == 1,
       {"before": open_, "after": past})
    # Timestamps are whole seconds, so a 1.4s wait can round to zero elapsed.
    # Move the clock instead of racing it, and check the arithmetic directly.
    ov = ledger.overdue_seconds(now=now_iso(3600))
    ok("C034c with the age of the oldest overdue forecast reported",
       ov is not None and 3000 < ov < 4000, ov)


def c035_two_forecasters_are_scored_on_the_same_items():
    from attest.decisions import now_iso
    from attest.forecast import BaseRate, Constant, Item, record_forecasts
    svc, d = tmp_service()
    ledger = svc.decisions
    hist = {"t0": [True] * 10, "t1": [False] * 10}
    items = [Item("t0", "q?"), Item("t1", "q?")]
    rec = record_forecasts(ledger, items, [Constant(0.5), BaseRate(hist)],
                           cohort="x1", due_at=now_iso(600))
    ok("C035 every forecaster answers every item", rec["recorded"] == 4, rec)
    rows = ledger.pending()
    ok("C035b and they share one cohort and one due time, so they are compared on "
       "the same question rather than on adjacent ones",
       len({r["cohort"] for r in rows}) == 1 and len({r["due_at"] for r in rows}) == 1,
       [(r["cohort"], r["due_at"]) for r in rows])
    ok("C035c the base rate is smoothed, so ten out of ten does not buy a claim "
       "of certainty",
       all(r["probability"] < 1.0 for r in rows if r["model"] == "base-rate"),
       [r["probability"] for r in rows if r["model"] == "base-rate"])


def c043_an_unchecked_host_is_not_an_untrusted_one():
    """An assertion that could not be measured is dropped, not treated as false. A
    host nobody could reach and a host that answered and was found wanting are
    different facts, and collapsing them is how a broken probe becomes a bad
    reputation."""
    from attest.trust import Assertion, assertions_to_items
    a = [Assertion("h1", "k1", "port_open", True),
         Assertion("h1", "k2", "port_open", None),      # could not measure
         Assertion("h1", "k3", "port_open", False)]
    items = assertions_to_items(a, 360)
    ok("C043 an assertion that could not be measured is dropped, not made false",
       len(items) == 2, [i.target for i in items])
    ok("C043b and the measurable ones keep their real values",
       sorted(i.target for i in items) == ["h1|k1", "h1|k3"], items)


def c044_a_host_without_evidence_is_excluded_not_ranked_last():
    """Quietly ordering an unmeasured host last is how an estate comes to believe it
    has compared three machines when it has compared two."""
    from attest.trust import host_stability, route
    hist, host_of = {}, {}
    for i in range(6):
        hist["steady|k%d" % i] = [True] * 9
        host_of["steady|k%d" % i] = "steady"
    hist["unknown|k0"] = [True]
    host_of["unknown|k0"] = "unknown"

    st = host_stability(hist, host_of)
    r = route(["steady", "unknown", "never-heard-of-it"], st, require_pairs=4)
    ok("C044 a host with no verification history is EXCLUDED, not ranked last",
       r["chosen"] == "steady"
       and sorted(x["host"] for x in r["excluded"]) == ["never-heard-of-it",
                                                        "unknown"],
       r["excluded"])
    ok("C044b and the exclusion says why, with the count it needed",
       all("needed to rank" in x["why"] for x in r["excluded"]), r["excluded"])

    # And when NOBODY has evidence, routing refuses rather than picking one anyway.
    r2 = route(["a", "b"], host_stability({}, {}), require_pairs=4)
    ok("C044c with no evidence anywhere, routing refuses rather than guessing",
       r2["chosen"] is None and "NO HOST HAS ENOUGH EVIDENCE" in r2["verdict"],
       r2["verdict"][:120])


def c050_a_run_can_succeed_and_still_cover_less():
    """Found in the real ledger before it was written here.

    Two panel cohorts recorded 13 targets where every other run recorded 14, because
    one item could not be measured. The run exited 0, stamped its heartbeat and its
    beat, and recorded a cohort. fleet_coverage saw a cohort and called it complete.
    Nothing said a word - only a human comparing cohort sizes would ever notice.

    A DROP is the signal, not a difference: the sweep's target universe grows as new
    timers appear, so 40 then 43 is healthy and must not be flagged."""
    sys.path.insert(0, os.path.join(ROOT, "tools"))
    from fleet_coverage import analyse
    P = {"quarterly": 900}
    series = {"quarterly": ["2026-10-02T10:00:00Z", "2026-10-02T10:15:00Z",
                            "2026-10-02T10:30:00Z"]}
    now = "2026-10-02T10:35:00Z"

    dropped = {"quarterly": [("2026-10-02T10:00:00Z", 14),
                             ("2026-10-02T10:15:00Z", 13),
                             ("2026-10-02T10:30:00Z", 13)]}
    rep, probs = analyse(series, P, now=now, targets=dropped)
    ok("C050 a run that covered 13 items where the one before had 14 is PARTIAL",
       rep[0]["verdict"] == "PARTIAL" and len(rep[0]["partial"]) == 1, rep[0])
    ok("C050b and the problem names both counts, and which dimension moved",
       probs and "13 items where the run before it had 14" in probs[0]
       and rep[0]["partial"][0]["what"] == "items", probs[:1])

    grew = {"quarterly": [("2026-10-02T10:00:00Z", 40),
                          ("2026-10-02T10:15:00Z", 43),
                          ("2026-10-02T10:30:00Z", 43)]}
    rep2, probs2 = analyse(series, P, now=now, targets=grew)
    ok("C050c while a target universe that GREW is not flagged - the sweep's does "
       "exactly that", rep2[0]["verdict"] == "complete" and not probs2, rep2[0])

    rep3, probs3 = analyse(series, P, now=now)
    ok("C050d and with no item data at all nothing changes",
       rep3[0]["verdict"] == "complete" and not probs3, rep3[0]["verdict"])

    known = {"quarterly|partial|items|2026-10-02T10:00:00Z|2026-10-02T10:15:00Z"}
    rep4, probs4 = analyse(series, P, now=now, targets=dropped, known=known)
    ok("C050e and an already-reported drop does not re-alert forever, the same rule "
       "as a gap", not probs4 and rep4[0]["partial"][0]["already_reported"] is True,
       rep4[0]["partial"])

    # Count targets alone and a FORECASTER can drop out invisibly: the panel still
    # covers every target with the baselines, so decisions fall 56 -> 42 while the
    # target count does not move. Demonstrated before the second dimension existed.
    lost_model = {"quarterly": [("2026-10-02T10:00:00Z", 14, 4),
                                ("2026-10-02T10:15:00Z", 14, 3)]}
    rep5, probs5 = analyse(series, P, now=now, targets=lost_model)
    ok("C050f a FORECASTER dropping out is caught too, not only a target",
       rep5[0]["verdict"] == "PARTIAL"
       and rep5[0]["partial"][0]["what"] == "forecasters"
       and "3 forecasters where the run before it had 4" in probs5[0], probs5[:1])

    both_grew = {"quarterly": [("2026-10-02T10:00:00Z", 40, 3),
                               ("2026-10-02T10:15:00Z", 43, 4)]}
    rep6, probs6 = analyse(series, P, now=now, targets=both_grew)
    ok("C050g while growth in both dimensions stays healthy",
       rep6[0]["verdict"] == "complete" and not probs6, rep6[0]["verdict"])

    two_tuple = {"quarterly": [("2026-10-02T10:00:00Z", 14),
                               ("2026-10-02T10:15:00Z", 13)]}
    _rep7, probs7 = analyse(series, P, now=now, targets=two_tuple)
    ok("C050h and the older two-element form still works",
       len(probs7) == 1, probs7)


def c049_a_detector_that_repeats_itself_forever_is_a_detector_that_gets_ignored():
    """Written after the coverage check reported its own finding.

    A gap is permanent - the 15:00 panel gap of 2026-10-02 is still in the ledger a
    year later. The first version failed on any gap it found, so it exited 1 forever,
    job_liveness reported "its last run exited 1" forever, and the notification fired
    every six hours about something already fixed. Found by running job_liveness and
    seeing the coverage job itself in the findings:

        crontab:zixen15 [20 */6 * * * zixen15]  failed  its last run exited 1

    A STALL is different and must never be suppressed: it is true right now."""
    sys.path.insert(0, os.path.join(ROOT, "tools"))
    from fleet_coverage import analyse
    P = {"quarterly": 900}
    gappy = {"quarterly": ["2026-10-02T10:00:00Z", "2026-10-02T10:15:00Z",
                           "2026-10-02T10:45:00Z"]}

    _r, first = analyse(gappy, P, now="2026-10-02T10:50:00Z")
    ok("C049 a gap fails the check the first time it is seen", len(first) == 1, first)

    known = {"quarterly|2026-10-02T10:15:00Z|2026-10-02T10:45:00Z"}
    rep, again = analyse(gappy, P, now="2026-10-02T10:50:00Z", known=known)
    ok("C049b and does NOT fail again on every subsequent run, forever",
       again == [], again)
    ok("C049c while still appearing in the report, marked as already reported",
       rep[0]["gaps"][0].get("already_reported") is True, rep[0]["gaps"])

    stalled = {"quarterly": ["2026-10-02T08:00:00Z"]}
    srep, sprob = analyse(stalled, P, now="2026-10-02T12:00:00Z", known=known)
    ok("C049d but a STALL is never suppressed - it is true now, not a mark in the "
       "past", srep[0]["verdict"] == "STALLED" and len(sprob) == 1, sprob)

    # A NEW gap on top of a known one must still be caught.
    two = {"quarterly": ["2026-10-02T10:00:00Z", "2026-10-02T10:15:00Z",
                         "2026-10-02T10:45:00Z", "2026-10-02T11:00:00Z",
                         "2026-10-02T11:45:00Z"]}
    _r2, fresh = analyse(two, P, now="2026-10-02T11:50:00Z", known=known)
    ok("C049e a second, different gap is still reported",
       len(fresh) == 1 and "11:00:00Z -> 2026-10-02T11:45:00Z" in fresh[0], fresh)


def c053_a_slow_forecaster_must_not_hold_the_write_lock():
    """Found by asking where the model call sits relative to the transaction.

    record_forecasts records inside its prediction loop, so the FIRST insert opens a
    write transaction and the loop then calls the NEXT forecaster - and the model
    forecaster blocks on an HTTP call for up to its timeout, 840 seconds here. So
    batching, introduced to REDUCE contention, had introduced a fourteen-minute writer
    lock in the model stratum.

    Measured before the fix, with commit=False held open the way that loop held it:

        second writer FAILED after 30.0s: OperationalError: database is locked

    Predicting everything first means the slow call happens before anything is
    written."""
    d = tempfile.mkdtemp(prefix="attest_slow_")
    db = os.path.join(d, "s.db")
    Store(db)

    slow = os.path.join(d, "slow.py")
    with open(slow, "w") as fh:
        fh.write("import sys, time\n"
                 "sys.path.insert(0, %r)\n"
                 "from attest import Store, DecisionLedger\n"
                 "from attest.decisions import now_iso\n"
                 "from attest.forecast import Constant, Forecaster, Item, record_forecasts\n"
                 "class Slow(Forecaster):\n"
                 "    name = 'slow'\n"
                 "    def predict(self, item, history=None):\n"
                 "        time.sleep(8)          # stands in for the model call\n"
                 "        return 0.5\n"
                 "l = DecisionLedger(Store(sys.argv[1]))\n"
                 "items = [Item('t%%d' %% i, 'q?', stratum='slow') for i in range(3)]\n"
                 "record_forecasts(l, items, [Constant(0.5, 'fast'), Slow()],\n"
                 "                cohort='slow', due_at=now_iso(600))\n" % ROOT)

    p = subprocess.Popen([sys.executable, slow, db],
                         stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    time.sleep(2)                     # let it get past the first fast forecast
    con = sqlite3.connect(db, timeout=5)   # five seconds of patience, not thirty
    blocked = None
    try:
        con.execute("INSERT INTO decisions (id, at, qtype, answer, stratum) "
                    "VALUES ('dec:other','2026-10-02T00:00:00Z','noul','a','other')")
        con.commit()
    except sqlite3.OperationalError as exc:
        blocked = str(exc)
    finally:
        con.close()
    _o, err = p.communicate(timeout=120)
    ok("C053 while a slow forecaster is running, another writer still gets in",
       blocked is None and p.returncode == 0,
       {"blocked": blocked, "slow_rc": p.returncode, "err": (err or b"").decode()[-160:]})

    # The negative half: the OLD shape - a transaction held open across the slow
    # call - must still block a writer, or the check above proves nothing.
    d2 = tempfile.mkdtemp(prefix="attest_slow2_")
    db2 = os.path.join(d2, "s.db")
    Store(db2)
    holder = sqlite3.connect(db2, timeout=30)
    holder.execute("BEGIN IMMEDIATE")
    holder.execute("INSERT INTO decisions (id, at, qtype, answer, stratum) "
                   "VALUES ('dec:held','2026-10-02T00:00:00Z','noul','a','held')")
    other = sqlite3.connect(db2, timeout=5)
    still_blocked = None
    try:
        other.execute("INSERT INTO decisions (id, at, qtype, answer, stratum) "
                      "VALUES ('dec:x','2026-10-02T00:00:00Z','noul','a','x')")
        other.commit()
    except sqlite3.OperationalError as exc:
        still_blocked = str(exc)
    finally:
        other.close()
        holder.rollback()
        holder.close()
    ok("C053b and the OLD shape - a transaction held open across the slow call - does "
       "still block, so the check above is not passing by accident",
       still_blocked is not None and "locked" in still_blocked, still_blocked)


def c052_the_cure_has_a_cost_and_it_is_measured():
    """WAL traded a loud failure for a quiet one, and this is the quiet one.

    Measured on 2026-10-02 with a reader holding an open snapshot: writes all
    succeeded, and the WAL grew ~26 KB per written decision, 5 MB -> 26 MB over a
    thousand writes, because a checkpoint cannot advance past the oldest live
    snapshot. Without WAL that reader would have BLOCKED the writers, which the
    fleet now detects and shouts about. With WAL the writers keep succeeding and the
    disk fills with nothing saying a word.

    Released, one wal_checkpoint(TRUNCATE) took it from 26 MB to zero with the rows
    and the integrity intact - so the condition is recoverable and merely unnoticed.
    """
    sys.path.insert(0, os.path.join(ROOT, "tools"))
    from fleet_coverage import analyse, WAL_WARN_MB
    P = {"hourly": 3600}
    s = {"hourly": ["2026-10-02T10:00:00Z", "2026-10-02T11:00:00Z"]}
    now = "2026-10-02T11:05:00Z"

    def wal(mb):
        rep, probs = analyse(s, P, now=now, wal_bytes=int(mb * 1048576))
        entry = [x for x in rep if x["stratum"] == "(ledger wal)"][0]["wal"]
        return entry, probs

    e1, p1 = wal(4)
    ok("C052 a WAL at its normal steady state is not flagged",
       e1["verdict"] == "normal" and not p1, e1)

    e2, p2 = wal(WAL_WARN_MB * 3)
    ok("C052b one that has grown to three times the threshold is",
       e2["verdict"] == "WAL TOO LARGE" and len(p2) == 1, e2)
    ok("C052c and the problem says what it means and what to do about it",
       "cannot checkpoint past the oldest live snapshot" in p2[0]
       and "wal_checkpoint(TRUNCATE)" in p2[0], p2[0][:120])

    # The same already-reported rule as gaps and partials.
    rep_k, probs_k = analyse(s, P, now=now, wal_bytes=int(WAL_WARN_MB * 3 * 1048576),
                             known={"wal|oversized"})
    entry_k = [x for x in rep_k if x["stratum"] == "(ledger wal)"][0]["wal"]
    ok("C052d and an oversized WAL already reported does not re-alert every run",
       not probs_k and entry_k["already_reported"] is True, entry_k)


def c051_the_real_write_paths_survive_each_other():
    """C047 spawns five identical synthetic writers. That proves the timeout works
    and says nothing about the three DIFFERENT write shapes the estate actually
    uses, which take the lock in different ways:

        a batched forecast  - record_forecasts, one commit for the whole batch
        per-decision writes - record(), one commit and one audit row each
        repeated opens      - Store(), which runs executescript(SCHEMA) every time

    Run together against one database, they must all finish and lose nothing. This
    is the shape that broke for real on 2026-10-02, when a panel run died with
    "database is locked" while a batch of forecasts was being recorded."""
    d = tempfile.mkdtemp(prefix="attest_mix_")
    db = os.path.join(d, "m.db")
    Store(db)

    batched = os.path.join(d, "batched.py")
    with open(batched, "w") as fh:
        fh.write("import sys\n"
                 "sys.path.insert(0, %r)\n"
                 "from attest import Store, DecisionLedger\n"
                 "from attest.decisions import now_iso\n"
                 "from attest.forecast import Constant, Item, record_forecasts\n"
                 "l = DecisionLedger(Store(sys.argv[1]))\n"
                 "items = [Item('b%%d' %% i, 'q?', stratum='batched')\n"
                 "         for i in range(100)]\n"
                 "record_forecasts(l, items, [Constant(0.5, 'n1'),\n"
                 "                            Constant(0.9, 'n2')],\n"
                 "                cohort='batched', due_at=now_iso(600))\n" % ROOT)

    percall = os.path.join(d, "percall.py")
    with open(percall, "w") as fh:
        fh.write("import sys\n"
                 "sys.path.insert(0, %r)\n"
                 "from attest import Store, DecisionLedger\n"
                 "from attest.decisions import now_iso\n"
                 "l = DecisionLedger(Store(sys.argv[1]))\n"
                 "for i in range(200):\n"
                 "    l.record('per-decision', 'a', qtype='noul',\n"
                 "             probability=0.5, due_at=now_iso(600),\n"
                 "             stratum='percall', commit=True)\n" % ROOT)

    opener = os.path.join(d, "opener.py")
    with open(opener, "w") as fh:
        fh.write("import sys\n"
                 "sys.path.insert(0, %r)\n"
                 "from attest import Store\n"
                 "for i in range(25):\n"
                 "    Store(sys.argv[1])\n" % ROOT)

    procs = [(os.path.basename(p), subprocess.Popen(
        [sys.executable, p, db], stdout=subprocess.PIPE, stderr=subprocess.PIPE))
        for p in (batched, percall, opener)]
    bad = []
    for name, p in procs:
        _o, e = p.communicate(timeout=300)
        if p.returncode != 0:
            bad.append((name, (e or b"").decode()[-160:]))

    con = sqlite3.connect(db)
    counts = dict(con.execute(
        "SELECT stratum, COUNT(*) FROM decisions GROUP BY stratum").fetchall())
    ok("C051 all three real write shapes finish together and none loses anything",
       not bad and counts.get("batched") == 200 and counts.get("percall") == 200,
       {"errors": bad, "counts": counts})
    ok("C051b and the database is intact afterwards",
       con.execute("PRAGMA integrity_check").fetchone()[0] == "ok", counts)
    con.close()


def c048_an_instrument_must_not_be_blind_to_its_own_silence():
    """The coverage check compares consecutive cohorts, so it can only see a gap
    BETWEEN two runs. A stratum that stops entirely writes no further cohorts, so
    there is no pair to compare and it reported "complete" forever.

    Demonstrated on a real timeline before fixing it: three hourly cohorts ending at
    12:00, read at 20:00, came back "complete" while the job had been dead for eight
    hours. That is the same defect the file exists to catch, one level up."""
    sys.path.insert(0, os.path.join(ROOT, "tools"))
    from fleet_coverage import analyse
    P = {"hourly": 3600, "quarterly": 900}

    stopped = {"hourly": ["2026-10-02T10:00:00Z", "2026-10-02T11:00:00Z",
                          "2026-10-02T12:00:00Z"]}
    rep, probs = analyse(stopped, P, now="2026-10-02T20:00:00Z")
    hourly = [r for r in rep if r["stratum"] == "hourly"][0]
    ok("C048 a stratum that stopped eight hours ago is reported STALLED, not "
       "complete", hourly["verdict"] == "STALLED", hourly)
    ok("C048b and the problem names how long it has been quiet",
       probs and "produced nothing for 480.0 min" in probs[0], probs[:1])

    # The guard must not be always-on: the same series, read soon after, is fine.
    rep2, probs2 = analyse(stopped, P, now="2026-10-02T12:30:00Z")
    hourly2 = [r for r in rep2 if r["stratum"] == "hourly"][0]
    ok("C048c the same history read half an hour later is complete, so the check "
       "is not simply always firing",
       hourly2["verdict"] == "complete" and not probs2, hourly2["verdict"])

    # One cohort that is old is a stall, not "not enough runs to judge".
    rep3, _ = analyse({"hourly": ["2026-10-02T08:00:00Z"]}, P,
                      now="2026-10-02T20:00:00Z")
    ok("C048d a single cohort, long stale, is a stall rather than an absence of "
       "evidence", rep3[0]["verdict"] == "STALLED", rep3[0])

    # And the original job - a gap BETWEEN two runs - must still be caught.
    gappy = {"quarterly": ["2026-10-02T10:00:00Z", "2026-10-02T10:15:00Z",
                           "2026-10-02T10:45:00Z"]}
    rep4, probs4 = analyse(gappy, P, now="2026-10-02T10:50:00Z")
    q = [r for r in rep4 if r["stratum"] == "quarterly"][0]
    ok("C048e an internal gap is still found after adding the silence check",
       q["verdict"] == "GAPS" and q["gaps"][0]["missed"] == 1, q)


def c047_concurrent_writers_lose_nothing():
    """Measured on the real estate on 2026-10-02, not imagined.

    Four scheduled jobs write to one ledger. A panel run died with
    "sqlite3.OperationalError: database is locked" while a batch of 294 forecasts
    was being recorded, and that cycle was silently skipped - the heartbeat said
    rc=1 and nothing read it. The store now runs in WAL with an explicit busy
    timeout, and record_forecasts commits once per batch instead of once per
    decision, which removes most of the contention rather than tolerating it."""
    from attest.store import BUSY_TIMEOUT_MS
    d = tempfile.mkdtemp(prefix="attest_conc_")
    db = os.path.join(d, "c.db")
    Store(db)                                    # create the schema first

    worker = os.path.join(d, "w.py")
    with open(worker, "w") as fh:
        fh.write("import sys\n"
                 "sys.path.insert(0, %r)\n"
                 "from attest import Store, DecisionLedger\n"
                 "from attest.decisions import now_iso\n"
                 "l = DecisionLedger(Store(sys.argv[1]))\n"
                 "for i in range(int(sys.argv[2])):\n"
                 "    l.record('q', 'a', qtype='noul', probability=0.5,\n"
                 "             due_at=now_iso(600))\n" % ROOT)

    N, EACH = 5, 60
    procs = [subprocess.Popen([sys.executable, worker, db, str(EACH)],
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE)
             for _ in range(N)]
    errs = []
    for p in procs:
        _o, e = p.communicate(timeout=300)
        if p.returncode != 0:
            errs.append((e or b"").decode()[-200:])
    got = sqlite3.connect(db).execute(
        "SELECT COUNT(*) FROM decisions").fetchone()[0]
    ok("C047 %d concurrent writers recording %d decisions each lose none"
       % (N, EACH), got == N * EACH and not errs,
       {"rows": got, "expected": N * EACH, "errors": errs[:2]})

    # The negative half. If the same collision does NOT fail without a busy
    # timeout, then the check above is passing for some reason other than the one
    # being claimed, and it is decoration.
    holder = sqlite3.connect(db)
    holder.execute("BEGIN IMMEDIATE")            # take the write lock and keep it
    blocked = sqlite3.connect(db, timeout=0)     # no patience at all
    collided = False
    try:
        blocked.execute("INSERT INTO decisions (id, at, qtype) VALUES "
                        "('dec:collide','2026-01-01T00:00:00Z','noul')")
        blocked.commit()
    except sqlite3.OperationalError as exc:
        collided = "locked" in str(exc)
    finally:
        holder.rollback()
        blocked.close()
        holder.close()
    ok("C047b and the same collision with no busy timeout DOES fail, so the "
       "control is not passing by accident", collided)

    # Check the artefact, not the outcome: the settings must actually be applied.
    s = Store(db)
    jm = s.db.execute("PRAGMA journal_mode").fetchone()[0]
    bt = s.db.execute("PRAGMA busy_timeout").fetchone()[0]
    ok("C047c the ledger is in WAL and waits %d ms for a writer, not the "
       "library's unconsidered default" % BUSY_TIMEOUT_MS,
       str(jm).lower() == "wal" and bt == BUSY_TIMEOUT_MS, {"wal": jm, "busy": bt})


def c046_many_assertions_checked_twice_is_one_moment_in_time():
    """The same trap as many decisions inside one cohort, one layer down.

    Sixty-one assertions checked twice produce sixty-one observation pairs and exactly
    one transition. A stability figure computed from that looks identical to one
    computed from a week of evidence, and routing on it would be routing on a single
    moment dressed up as a measured reputation."""
    from attest.trust import host_stability, route
    hist, host_of = {}, {}
    for i in range(61):                      # a host with a LOT of assertions...
        hist["big|k%d" % i] = [True, True]   # ...checked exactly twice
        host_of["big|k%d" % i] = "big"
    st = host_stability(hist, host_of)
    ok("C046 sixty-one assertions checked twice is 61 pairs but only 1 check",
       st["big"]["observation_pairs"] == 61 and st["big"]["checks"] == 1, st["big"])
    r = route(["big"], st, require_pairs=4, require_checks=3)
    ok("C046b so the host is EXCLUDED despite having far more pairs than required",
       r["chosen"] is None and "check(s)" in r["excluded"][0]["why"], r["excluded"])
    ok("C046c and the exclusion names the count it actually lacked",
       "1 check(s), 3 needed" in r["excluded"][0]["why"], r["excluded"][0]["why"])

    # Once there are enough real checks, the same host becomes rankable.
    for i in range(61):
        hist["big|k%d" % i] = [True, True, True, True]
    st2 = host_stability(hist, host_of)
    ok("C046d with four checks the same host becomes rankable",
       route(["big"], st2, require_pairs=4, require_checks=3)["chosen"] == "big",
       st2["big"])


def c045_a_perfect_record_does_not_buy_certainty():
    """Laplace smoothing, for the same reason the base-rate forecaster has it: finite
    evidence cannot establish a probability of exactly one."""
    from attest.trust import host_stability, HostStability
    hist = {"a|k%d" % i: [True] * 40 for i in range(4)}
    host_of = {"a|k%d" % i: "a" for i in range(4)}
    st = host_stability(hist, host_of)
    ok("C045 a flawless host records a flip rate above zero",
       st["a"]["flip_rate"] > 0, st["a"])
    ok("C045b so its stability is high but strictly below certainty",
       0.9 < st["a"]["stable_probability"] < 1.0, st["a"]["stable_probability"])

    # And the naive baseline and the trust model must be able to disagree, or the
    # trust model is doing nothing the assumption was not already doing.
    obs = {k: True for k in hist}
    hs = HostStability(hist, obs, host_of)
    flappy = {"a|k0": [True, False] * 9}
    hs_f = HostStability(flappy, {"a|k0": True}, {"a|k0": "a"})
    from attest.trust import AlwaysHolds, Assertion
    item = Assertion("a", "k0", "port_open", True).item(360)
    ok("C045c and on an assertion with a bad history the two disagree sharply",
       hs_f.predict(item) < 0.5 < AlwaysHolds(obs).predict(item),
       {"trust": hs_f.predict(item), "naive": AlwaysHolds(obs).predict(item)})


def c042_resolution_is_the_thing_accuracy_cannot_see():
    """The daily sweep has one broken target in forty-one, so a forecaster that says
    "nothing will break" scores 97.6% accuracy. Every accuracy-shaped summary calls
    that excellent and it knows nothing whatsoever. The resolution term is the only
    one that says so, and without it the consequential stratum would be unscoreable."""
    from attest.forecast import brier_decomposition, brier, Constant

    # One true in forty. A constant forecaster at the base rate.
    rare = [(0.025, 1 if i == 0 else 0) for i in range(40)]
    d = brier_decomposition(rare)
    acc = sum(1 for _p, y in rare if y == 0 and _p < 0.5 or y == 1 and _p >= 0.5) / len(rare)
    ok("C042 a constant forecaster on a rare population scores high accuracy",
       acc >= 0.97, acc)
    ok("C042b and resolution EXACTLY zero, however good the accuracy looks",
       d["resolution"] == 0.0, d)

    # A forecaster that actually separates the rare case from the common one.
    sep = [(0.9, 1)] * 30 + [(0.1, 0)] * 10
    d2 = brier_decomposition(sep)
    ok("C042c while one that separates them has real resolution",
       d2["resolution"] > 0.1 and d2["reliability"] < 0.05, d2)

    # The decomposition must reconstruct the Brier score, or it is a story.
    from attest.forecast import Constant as _C, Item as _I
    for label, pairs in (("rare", rare), ("separating", sep),
                         ("mixed", [(_C(0.5).predict(_I("t", "q")), i % 3 == 0)
                                    for i in range(45)])):
        dd = brier_decomposition(pairs)
        ok("C042d [%s] reliability - resolution + uncertainty reconstructs the "
           "Brier score exactly" % label,
           abs(dd["brier"] - dd["reconstructed"]) < 1e-12,
           {"brier": dd["brier"], "reconstructed": dd["reconstructed"]})


def c041_a_pooled_score_may_not_hide_a_change_of_method():
    """Found on the real estate, and it nearly produced a false result.

    One cohort of the model's forecasts was recorded under a prompt I had broken,
    and it scored a Brier of 0.6429 while every other cohort scored 0.1429. Pooled,
    the forecaster looked far worse than it was on every batch that used the working
    prompt. A pooled number that hides a regime change is not a result - a cohort is
    a unit of METHOD as well as of correlation."""
    from attest.decisions import now_iso
    from attest.forecast import (Constant, Item, record_forecasts, resolve_due,
                                  head_to_head)
    svc, d = tmp_service()
    ledger = svc.decisions
    items = [Item("t%d" % i, "q?", stratum="s") for i in range(40)]

    # Cohort A: everything true, and the forecaster says 0.9. Nearly perfect.
    record_forecasts(ledger, items, [Constant(0.9, "m")], cohort="A",
                     due_at=now_iso(60))
    resolve_due(ledger, lambda dec: ("yes", True), now=now_iso(7200))
    # Cohort B: everything false, same forecaster. Catastrophic.
    record_forecasts(ledger, items, [Constant(0.9, "m")], cohort="B",
                     due_at=now_iso(60))
    resolve_due(ledger, lambda dec: ("no", False), now=now_iso(7200))

    h = head_to_head(ledger, ["m"], stratum="s")
    row = h["forecasters"][0]
    ok("C041 two cohorts scoring 0.01 and 0.81 are reported apart, not averaged "
       "into one number",
       row["brier_by_cohort"] == {"A": 0.01, "B": 0.81}, row["brier_by_cohort"])
    ok("C041b and the headline says the pooled figure hides a change of method",
       row["cohort_spread_warning"] is not None
       and "hides a change in method" in h["verdict"], h["verdict"][:220])

    # The negative half: cohorts that AGREE must not raise this.
    svc2, _ = tmp_service()
    l2 = svc2.decisions
    it2 = [Item("u%d" % i, "q?", stratum="s2") for i in range(40)]
    record_forecasts(l2, it2, [Constant(0.9, "m")], cohort="C", due_at=now_iso(60))
    resolve_due(l2, lambda dec: ("yes", True), now=now_iso(7200))
    record_forecasts(l2, it2, [Constant(0.9, "m")], cohort="D", due_at=now_iso(60))
    resolve_due(l2, lambda dec: ("yes", True), now=now_iso(7200))
    h2 = head_to_head(l2, ["m"], stratum="s2")
    ok("C041c while cohorts that agree do NOT trigger it - the guard is not "
       "simply always on",
       h2["forecasters"][0]["cohort_spread_warning"] is None,
       h2["forecasters"][0]["cohort_spread_warning"])


def c040_a_headline_may_not_overstate_the_sample_it_rests_on():
    """head_to_head said "Best Brier: persistence at 0.0244 over 40 resolved
    decisions" while calibration() on the same rows reported those 80 decisions
    came from 3 cohorts. The headline is what gets read; the caveat is what gets
    scrolled past. So the caveat goes in the headline."""
    from attest.decisions import now_iso
    from attest.forecast import (Constant, Item, record_forecasts, resolve_due,
                                  head_to_head)
    svc, d = tmp_service()
    ledger = svc.decisions
    items = [Item("t%d" % i, "q?", stratum="corr") for i in range(40)]
    record_forecasts(ledger, items, [Constant(0.9, "good")], cohort="one",
                     due_at=now_iso(60))
    # A MIXED population on purpose: all-true would be degenerate and take the
    # earlier branch of the verdict, which would test nothing about this rule.
    truth = {"t%d" % i: (i % 2 == 0) for i in range(40)}
    resolve_due(ledger,
                lambda dec: (str(truth[dec["target"]]), truth[dec["target"]]),
                now=now_iso(7200))
    h = head_to_head(ledger, ["good"], stratum="corr")
    v = h["verdict"]
    ok("C040 a ranking built on one batch says so in the headline, not only in "
       "the detail", "OPTIMISTIC" in v or "cohort" in v, v[:200])
    ok("C040b and it reports the cohort count beside the decision count",
       h["forecasters"][0]["cohorts"] == 1
       and h["forecasters"][0]["resolved"] == 40, h["forecasters"][0])
    ok("C040c with the effective sample spelled out",
       "at most 1 independent observation" in v, v[:240])


def c039_a_short_gap_is_not_reported_as_a_broken_resolver():
    """The detector caught itself crying wolf on the real estate.

    Two forecasts came due at 06:58:31 and the ledger called them a broken
    resolver at 07:03, because the item they were about happened to be
    unmeasurable in that one sample. Five minutes late at a fifteen-minute
    cadence is not a fault, and a detector that says it is trains the reader to
    ignore the channel - after which the real one is ignored too."""
    from attest.decisions import now_iso, OVERDUE_GRACE_S
    svc, d = tmp_service()
    ledger = svc.decisions
    ledger.record("q", "true", qtype="noul", probability=0.5, due_at=now_iso(1))
    time.sleep(1.4)
    open_ = ledger.calibration()["open"]
    ok("C039 a forecast that came due moments ago is NOT a broken resolver",
       open_["due_but_unrecorded"] == 0 and open_["due_just_now"] == 1, open_)
    ok("C039b and the grace window is reported rather than left implicit",
       open_["grace_seconds"] == int(OVERDUE_GRACE_S), open_)
    # Wind the clock past the grace window: now the same decision IS a fault.
    past = ledger.calibration(now=now_iso(OVERDUE_GRACE_S + 60))["open"]
    ok("C039c but one left unrecorded past the grace window still is",
       past["due_but_unrecorded"] == 1 and past["due_just_now"] == 0, past)


def c038_the_recorded_answer_agrees_with_the_probability():
    """A row that answers "true" while carrying p=0.005 asserts a proposition and
    denies it in the same breath. Nothing downstream reads the answer field, which
    is exactly why nobody would notice - and why a table full of them would still
    look like evidence."""
    from attest.decisions import now_iso
    from attest.forecast import Constant, Item, record_forecasts
    svc, d = tmp_service()
    ledger = svc.decisions
    items = [Item("t%d" % i, "q?") for i in range(6)]
    record_forecasts(ledger, items, [Constant(0.05, "low"), Constant(0.95, "high")],
                     cohort="ans", due_at=now_iso(600))
    rows = ledger.pending()
    low = [r for r in rows if r["model"] == "low"]
    high = [r for r in rows if r["model"] == "high"]
    ok("C038 a forecaster at 0.05 records its answer as false, not true",
       low and all(r["answer"] == "false" for r in low),
       [(r["answer"], r["probability"]) for r in low])
    ok("C038b and one at 0.95 records true",
       high and all(r["answer"] == "true" for r in high),
       [(r["answer"], r["probability"]) for r in high])
    ok("C038c so the answer never contradicts the probability on the same row",
       all((r["answer"] == "true") == (r["probability"] >= 0.5) for r in rows), rows)


def c036_an_open_forecast_is_not_a_correct_one():
    from attest.decisions import now_iso
    from attest.forecast import Constant, Item, record_forecasts
    svc, d = tmp_service()
    ledger = svc.decisions
    items = [Item("t%d" % i, "q?") for i in range(40)]
    record_forecasts(ledger, items, [Constant(0.95)], cohort="o1",
                     due_at=now_iso(3600))
    cal = ledger.calibration(model="constant-0.95")
    ok("C036 forty forecasts still waiting are reported unresolved, not as forty "
       "correct", cal["resolved"] == 0 and cal["unresolved"] == 40, cal)
    ok("C036b and no rate is claimed from them",
       "INSUFFICIENT DATA" in cal["verdict"], cal["verdict"])
    dis = ledger.discriminability(model="constant-0.95")
    ok("C036c and discriminability declines to judge before the outcomes exist",
       dis["can_demonstrate_skill"] is None, dis)


def c054_a_refuted_assertion_is_not_fed_to_the_router_as_true():
    """The fleet store keeps the value an assertion had WHEN CLAIMED and the verdict
    on it now, in the same row, and does not keep observed_now. Reading the claim
    alone feeds a refuted assertion to the routing model as TRUE and a recovered one
    as FALSE. That fault is invisible until something breaks, which is the moment the
    router matters most.

    Measured on throne 2026-10-02: tools/fleet_trust.py read row["observed"] alone.
    """
    from attest.trust import Assertion, current_value
    ok("C054 a refuted assertion is read as FALSE, not as its stale claim",
       current_value(True, "refuted") is False, current_value(True, "refuted"))
    ok("C054b a recovered assertion is read as TRUE, not as its stale claim",
       current_value(False, "recovered") is True, current_value(False, "recovered"))
    ok("C054c a stale row keeps its value; ageing is not change",
       current_value(True, "stale") is True and current_value(False, "stale") is False,
       "stale True->True and stale False->False")
    ok("C054d never_held keeps False",
       current_value(False, "never_held") is False, "")
    ok("C054e an unmeasurable row has no value, not a false one",
       current_value(True, "unmeasurable") is None, "")
    ok("C054f CONTROL a row with no status is taken at face value, not flipped",
       current_value(True, None) is True and current_value(False, None) is False,
       "an unknown source does not invent a change")
    naive = {"observed": True, "status": "refuted"}
    ok("C054g NEGATIVE the old reading really is wrong on this row",
       naive["observed"] is not current_value(naive["observed"], naive["status"]),
       "row observed=True while the verdict says it no longer holds")
    ok("C054h an aged assertion is not a CURRENT observation",
       Assertion("h", "k", "port_open", True, status="stale").current is False
       and Assertion("h", "k", "port_open", True, status="fresh").current is True
       and Assertion("h", "k", "port_open", None, status="fresh").current is False,
       "stale->False, fresh->True, unmeasured->False")

CONTROLS = [c001_a_bare_bool_cannot_carry_unknown,
            c002_anti_blindness_timeout_is_unknown,
            c003_a_real_failure_is_disputed,
            c004_signal_death_is_unknown,
            c005_no_path_to_verified_without_a_checker,
            c006_the_auditor_can_actually_complain,
            c007_only_verified_excludes_asserted,
            c008_tier_survives_the_round_trip,
            c009_untiered_facts_are_impossible,
            c010_verified_outranks_asserted,
            c011_kind_aliases_and_refusal,
            c012_unknown_kind_fails_rather_than_defaulting,
            c013_absent_lean_is_unknown_not_disputed,
            c014_capabilities_is_measured_not_asserted,
            c015_graph_absence_is_unknown_and_contradiction_is_disputed,
            c016_memcheck_never_claims_health_from_silence,
            c017_warm_actually_waits,
            c018_missing_dependency_is_not_a_disproof,
            c019_a_judgment_with_no_confidence_is_refused,
            c020_an_unresolved_decision_is_not_a_correct_one,
            c021_a_decision_is_settled_once,
            c022_an_unknown_outcome_stays_unresolved,
            c023_overconfidence_is_detected_and_only_then,
            c024_a_well_calibrated_bucket_is_not_called_overconfident,
            c025_wilson_brackets_the_estimate,
            c037_an_existing_ledger_gains_the_prospective_columns,
            c026_a_forecast_must_be_about_the_future,
            c027_only_forecasts_come_due,
            c028_an_unresolvable_forecast_stays_open,
            c029_the_ledger_can_still_catch_a_bad_forecaster,
            c030_a_population_that_cannot_show_skill_is_named_as_such,
            c031_a_worse_than_base_rate_forecaster_scores_negative,
            c032_ranking_refuses_when_the_population_cannot_support_it,
            c033_many_samples_in_one_cohort_are_not_many_observations,
            c034_a_forecast_nobody_settled_is_a_broken_resolver,
            c035_two_forecasters_are_scored_on_the_same_items,
            c036_an_open_forecast_is_not_a_correct_one,
            c038_the_recorded_answer_agrees_with_the_probability,
            c039_a_short_gap_is_not_reported_as_a_broken_resolver,
            c040_a_headline_may_not_overstate_the_sample_it_rests_on,
            c041_a_pooled_score_may_not_hide_a_change_of_method,
            c042_resolution_is_the_thing_accuracy_cannot_see,
            c043_an_unchecked_host_is_not_an_untrusted_one,
            c044_a_host_without_evidence_is_excluded_not_ranked_last,
            c045_a_perfect_record_does_not_buy_certainty,
            c046_many_assertions_checked_twice_is_one_moment_in_time,
            c047_concurrent_writers_lose_nothing,
            c048_an_instrument_must_not_be_blind_to_its_own_silence,
            c049_a_detector_that_repeats_itself_forever_is_a_detector_that_gets_ignored,
            c050_a_run_can_succeed_and_still_cover_less,
            c051_the_real_write_paths_survive_each_other,
            c052_the_cure_has_a_cost_and_it_is_measured,
            c053_a_slow_forecaster_must_not_hold_the_write_lock,
            c054_a_refuted_assertion_is_not_fed_to_the_router_as_true]


def main():
    print("attest conformance controls")
    print("=" * 66)
    for fn in CONTROLS:
        try:
            fn()
        except Exception as exc:
            import traceback
            ok(fn.__name__, False, "%s: %s" % (type(exc).__name__, exc))
            traceback.print_exc()
    print("=" * 66)
    print("%d passed, %d failed, %d skipped" % (len(PASS), len(FAIL), len(SKIP)))
    if FAIL:
        print("FAILED: " + ", ".join(FAIL))
    if SKIP:
        print("SKIPPED (these are NOT passes): " + ", ".join(SKIP))
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
