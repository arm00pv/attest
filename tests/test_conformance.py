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
            c025_wilson_brackets_the_estimate]


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
