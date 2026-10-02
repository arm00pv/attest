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
    open_ = ledger.calibration()["open"]
    ok("C034 the ledger separates 'not knowable yet' from 'knowable and never "
       "written down' from 'was never a forecast'",
       open_["awaiting_outcome"] == 1 and open_["due_but_unrecorded"] == 1
       and open_["never_a_forecast"] == 1, open_)
    ok("C034b and calls a knowable outcome nobody recorded a broken resolver, "
       "not a slow one",
       "broken resolver" in ledger.calibration()["verdict"],
       ledger.calibration()["verdict"])
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
            c038_the_recorded_answer_agrees_with_the_probability]


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
