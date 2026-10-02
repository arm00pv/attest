"""The operations. Eleven of them, and the last four exist to keep the service honest.

    capabilities  what this machine can and cannot ACTUALLY check
    verify        put a claim against a real checker
    remember      leave a finding, with its epistemic tier attached
    recall        read findings back, still carrying that tier
    memcheck      test this machine's own RAM, because the checker runs on it
"""

from __future__ import annotations

import json
import subprocess
import sys
import threading
import time
from typing import Any, Dict, List, Optional

from . import checkers
from .checkers import GraphChecker
from .decisions import DecisionLedger
from .store import Store, TIER_ASSERTED, TIER_VERIFIED
from .verdict import Verdict

VERSION = "0.3.0"

# MEASURED on the machine this came from: capabilities() took 122 SECONDS,
# because it runs real probes, including compiling against Lean. That is the
# right thing for it to do and the wrong thing to make a caller wait for. So it
# is cached and refreshed out of band, and 'cache_age_seconds' is always
# reported so the staleness is VISIBLE rather than hidden.
CAPS_TTL = 900.0

PROBE_LEAN_CORE = "example : (1 : Nat) + 1 = 2 := by decide"
PROBE_LEAN_MATHLIB = "example : (1 : Nat) + 1 = 2 := by norm_num"


class Service:
    def __init__(self, db_path: Optional[str] = None):
        self.store = Store(db_path)
        self.graph = GraphChecker(self.store)
        # Named 'decisions', not 'ledger': the store already has a ledger() event
        # log, and two different things called ledger in one object is how you
        # write to the wrong one for a month without noticing.
        self.decisions = DecisionLedger(self.store)
        self._caps: Dict[str, Any] = {"data": None, "at": 0.0, "refreshing": False}
        self._lock = threading.Lock()
        self._op_lock = threading.Lock()

    # ------------------------------------------------------------ capabilities
    def _probe_code(self) -> Dict[str, Any]:
        v = checkers.check_code("print(2 + 2)", timeout=15)
        ok = v.verified and "4" in (v.detail.get("stdout") or "")
        return {"ok": ok, "seconds": round(v.seconds, 2),
                "detail": ("a subprocess ran and printed the right answer"
                           if ok else v.reason or "the probe did not return 4"),
                "sandbox": checkers.CODE_SANDBOX_NOTE}

    def _probe_graph(self) -> Dict[str, Any]:
        import uuid
        src = "probe:" + uuid.uuid4().hex[:8]
        t0 = time.time()
        try:
            self.graph.add(src, "IS", "reachable", tier="verified", who="capabilities")
            v = self.graph.check(src, "IS", "reachable")
            self.store.db.execute("DELETE FROM triples WHERE source=?", (src,))
            self.store.db.commit()
            return {"ok": v.verified, "seconds": round(time.time() - t0, 2),
                    "detail": ("wrote a triple and read it back"
                               if v.verified else v.reason)}
        except Exception as exc:
            return {"ok": False, "seconds": round(time.time() - t0, 2),
                    "detail": "%s: %s" % (type(exc).__name__, exc)}

    def _probe_math(self) -> Dict[str, Any]:
        lib = checkers.mathlib_dir()
        if not checkers._lean_binary() and not checkers._lake_binary():
            return {"ok": False, "seconds": 0.0, "present": False,
                    "mathlib": bool(lib),
                    "detail": "no Lean toolchain on PATH",
                    "verdict": "NO LEAN BINARY - math claims cannot be checked here. "
                               "kind='code' and kind='graph' still work."}
        src = PROBE_LEAN_MATHLIB if lib else PROBE_LEAN_CORE
        v = checkers.check_math(src, timeout=180)
        if v.verified:
            verdict = ("MEASURED OK - a %s claim verified in %.1fs"
                       % ("Mathlib" if lib else "core Lean", v.seconds))
        elif v.checker_error:
            verdict = ("LEAN IS INSTALLED BUT DID NOT REACH A VERDICT (%s) - math "
                       "claims cannot be relied on here right now" % v.reason)
        else:
            verdict = ("LEAN RAN AND REJECTED THE TRIVIAL PROBE (%s) - the toolchain "
                       "is present but not usable" % v.reason)
        return {"ok": v.verified, "seconds": round(v.seconds, 2), "present": True,
                "mathlib": bool(lib), "probe": src, "detail": v.reason or "verified",
                "verdict": verdict}

    def _measure(self) -> Dict[str, Any]:
        caps: Dict[str, Any] = {
            "service": "attest",
            "version": VERSION,
            "measured_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "checks": {},
        }
        # Measured, never asserted. The system this came from once reported
        # "GROUND TRUTH AVAILABLE" on the strength of os.path.isdir(MATHLIB).
        # The directory did exist. Lean could not check a single proof. A
        # capability verdict derived from a filesystem lookup is the exact thing
        # capabilities() exists to prevent.
        caps["checks"]["code"] = self._probe_code()
        caps["checks"]["graph"] = self._probe_graph()
        caps["checks"]["math"] = self._probe_math()
        caps["checks"]["store"] = {
            "path": self.store.path,
            "counts": self.store.counts(),
            "ledger_entries": self.store.ledger_count(),
            "retrieval": "token match over the text, deliberately - NOT a vector "
                         "index. This service adds provenance, not similarity.",
        }
        caps["checks"]["python"] = {"version": sys.version.split()[0],
                                    "executable": sys.executable}
        usable = [k for k in ("math", "code", "graph")
                  if caps["checks"][k].get("ok")]
        unusable = [k for k in ("math", "code", "graph")
                    if not caps["checks"][k].get("ok")]
        caps["verdict"] = (
            "can check: %s" % ", ".join(usable) if usable else
            "NOTHING CAN BE CHECKED ON THIS MACHINE - every verify() here will "
            "return UNKNOWN")
        if unusable:
            caps["verdict"] += " | cannot check: %s" % ", ".join(unusable)
        return caps

    def capabilities(self) -> Dict[str, Any]:
        with self._lock:
            data, at, refreshing = self._caps["data"], self._caps["at"], self._caps["refreshing"]
        if data is None:
            if not refreshing:
                threading.Thread(target=self._refresh_caps, daemon=True).start()
            # 'ok' is False here and that is a trap: a caller that reads only
            # 'ok' - which is most callers - would conclude the machine cannot
            # check anything. It has not been ASKED yet. So the state is named
            # explicitly and the verdict says what it is not.
            return {"ok": False, "warming": True, "measured": False,
                    "verdict": "NOT YET MEASURED - this is not a failure and not a "
                               "result. Retry shortly; the manifest needs no "
                               "measurement at all.",
                    "error": "capabilities are being measured for the first time.",
                    "manifest": "/attest/v1/manifest"}
        out = dict(data)
        out["ok"] = True
        out["measured"] = True
        age = time.time() - at
        out["cache_age_seconds"] = round(age, 1)
        out["stale"] = age > CAPS_TTL
        if out["stale"]:
            threading.Thread(target=self._refresh_caps, daemon=True).start()
        return out

    def _refresh_caps(self) -> None:
        with self._lock:
            if self._caps["refreshing"]:
                return
            self._caps["refreshing"] = True
        try:
            data = self._measure()
            with self._lock:
                self._caps["data"] = data
                self._caps["at"] = time.time()
        finally:
            with self._lock:
                self._caps["refreshing"] = False

    def warm(self, timeout: float = 300.0) -> bool:
        """Measure capabilities and WAIT for the measurement.

        Not fire-and-forget. The first version called _refresh_caps(), which
        returns immediately when another refresh is already in flight - so a
        synchronous caller asked for a measurement, got the warming state back,
        and printed "NOT YET MEASURED". Waiting is the entire point of a
        synchronous call.
        """
        if self._caps["data"] is None:
            threading.Thread(target=self._refresh_caps, daemon=True).start()
        deadline = time.time() + timeout
        while self._caps["data"] is None and time.time() < deadline:
            time.sleep(0.2)
        return self._caps["data"] is not None

    # ------------------------------------------------------------------ verify
    def verify(self, claim: str, kind: Optional[str] = None, who: str = "",
               record: bool = True) -> Dict[str, Any]:
        if not claim or not str(claim).strip():
            return {"ok": False, "error": "claim is required"}
        raw = str(claim)
        k = checkers.resolve_kind(kind, raw)
        if k is None:
            return {"ok": False, "error": "unknown kind: %s" % kind,
                    "accepted": list(checkers.KINDS),
                    "aliases": sorted(checkers.KIND_ALIASES)}
        body = checkers.strip_prefix(raw) if kind is None else raw

        if k == "math":
            v = checkers.check_math(body)
        elif k == "code":
            v = checkers.check_code(body)
        else:
            parts = [p.strip() for p in body.split("|")]
            if len(parts) != 3:
                v = Verdict.unknown("graph", "a graph claim needs "
                                             "source|relation|target")
            else:
                v = self.graph.check(*parts)

        out = v.to_dict()
        out["kind"] = k
        out["claim"] = raw[:200]
        out["who"] = who
        # The rule, restated at the boundary where a caller reads it. A timeout
        # or a crashed checker is UNKNOWN; only a checker that ran may dispute.
        if v.checker_error:
            out["interpretation"] = ("UNKNOWN - no checker reached a verdict. This "
                                     "is NOT a disproof of the claim.")
        elif v.verified:
            out["interpretation"] = "VERIFIED - a real checker passed this claim."
        else:
            out["interpretation"] = "DISPUTED - a real checker ran and rejected it."
        self.store.ledger("verify", "kind=%s outcome=%s who=%s" % (k, v.outcome, who))
        if record:
            self.remember(
                ("verified %s claim: %s" % (k, raw[:160])) if v.verified else
                ("UNKNOWN %s claim (no verdict reached): %s" % (k, raw[:160]))
                if v.checker_error else
                ("DISPUTED %s claim: %s" % (k, raw[:160])),
                why=json.dumps({"checker": v.checker, "reason": v.reason,
                                "seconds": round(v.seconds, 3)})[:400],
                who=who, verified=v.verified, checker=v.checker,
                domain="attest_verification")
        return out

    # ---------------------------------------------------------------- remember
    def remember(self, fact: str, why: str = "", who: str = "",
                 verified: bool = False, checker: str = "",
                 domain: str = "") -> Dict[str, Any]:
        if not fact or not str(fact).strip():
            return {"ok": False, "error": "fact is required"}
        out = self.store.remember(str(fact), why=why, who=who,
                                  verified=bool(verified), checker=checker,
                                  domain=domain)
        out["interpretation"] = (
            "stored as VERIFIED - a checker passed this"
            if out.get("tier") == TIER_VERIFIED else
            "stored as ASSERTED - nothing checked this, and recall() will say so")
        return out

    # ------------------------------------------------------------------ recall
    def recall(self, query: str, limit: int = 5,
               only_verified: bool = False) -> Dict[str, Any]:
        if not query or not str(query).strip():
            return {"ok": False, "error": "query is required"}
        try:
            limit = max(1, min(int(limit or 5), 100))
        except Exception:
            return {"ok": False, "error": "limit must be an integer"}
        return self.store.recall(str(query), limit=limit,
                                 only_verified=bool(only_verified))

    # ---------------------------------------------------------------- memcheck
    def memcheck(self, gb: int = 2, passes: int = 2) -> Dict[str, Any]:
        """Test this machine's RAM for reproducible bit errors, right now.

        This is not a novelty. The checker runs on this machine, so if the memory
        is corrupting pages then the CHECKER can be corrupted, and a verdict from
        here is not trustworthy - including the verdicts this service returns.
        Saying so is the whole point of reporting capabilities at all.
        """
        import ctypes
        import struct
        try:
            gb = max(1, min(int(gb or 2), 12))
            passes = max(1, min(int(passes or 2), 5))
        except Exception:
            return {"ok": False, "error": "gb and passes must be integers"}
        PAGE = 4096
        npages = (gb * 1024 ** 3) // PAGE
        cache: Dict[int, bytes] = {}
        buf = ctypes.create_string_buffer(npages * PAGE)
        base = ctypes.addressof(buf)
        try:
            for i in range(npages):
                ctypes.memmove(base + i * PAGE, struct.pack("<Q", i) * (PAGE // 8), PAGE)
            per_pass = []
            for _ in range(passes):
                bad = []
                for i in range(npages):
                    if i not in cache:
                        cache[i] = struct.pack("<Q", i) * (PAGE // 8)
                    if ctypes.string_at(base + i * PAGE, PAGE) != cache[i]:
                        bad.append(i)
                per_pass.append(bad)
            common = set(per_pass[0])
            for b in per_pass[1:]:
                common &= set(b)
            return {"ok": True, "tested_gb": gb, "passes": passes, "pages": npages,
                    "bad_per_pass": [len(b) for b in per_pass],
                    "reproducible_bad_pages": sorted(common)[:20],
                    "reproducible_count": len(common),
                    "verdict": (("FAILING MEMORY: %d pages fail in EVERY pass, at "
                                 "fixed offsets - treat every verdict from this "
                                 "machine as suspect" % len(common)) if common else
                                ("no reproducible fault in this %d GB allocation - "
                                 "this does NOT prove the memory is good; a fault "
                                 "outside this region is invisible to it" % gb))}
        finally:
            del buf, cache

    # --------------------------------------------------------------- decisions
    def record_decision(self, question: str, answer: str, qtype: str = "choice",
                        probability: Optional[float] = None,
                        confidence: Optional[float] = None, state: str = "",
                        model: str = "", who: str = "",
                        alternatives: Optional[Dict[str, Any]] = None,
                        due_at: Optional[str] = None, stratum: str = "",
                        cohort: str = "", target: str = "") -> Dict[str, Any]:
        """Log one decision WITH the confidence it was made at.

        A noul judgment with no probability is refused: a boolean with no
        confidence attached is precisely the unmeasured claim this ledger exists
        to catch. It is UNRESOLVED until an outcome is recorded, and an
        unresolved decision is excluded from every rate. Silence is not success.

        due_at is what makes it a FORECAST rather than a retrospective: the
        decision stays open until the world catches up, and a due_at that has
        already passed is refused outright.
        """
        return self.decisions.record(question, answer, qtype=qtype,
                                     probability=probability,
                                     confidence=confidence, state=state,
                                     model=model, who=who,
                                     alternatives=alternatives, due_at=due_at,
                                     stratum=stratum, cohort=cohort, target=target)

    def resolve_decision(self, decision_id: str, outcome: str = "",
                         correct: Optional[bool] = None) -> Dict[str, Any]:
        """Record what actually happened, once.

        'I do not know how it turned out' is not an outcome. It leaves the
        decision unresolved, which is a different and honest state. And a
        decision that can be quietly revised after the fact is not a prediction,
        so this refuses to resolve one twice.
        """
        return self.decisions.resolve(decision_id, outcome, correct)

    def calibration(self, model: Optional[str] = None,
                    stratum: Optional[str] = None) -> Dict[str, Any]:
        """How often was a decision taken at confidence p actually right?

        Reported per bucket with a Wilson interval, and it REFUSES to report a
        rate below MIN_N resolved decisions. Measured September 2026: 108 items
        could not separate a calibration error of 0.066 from 0.061, so printing a
        rate below a few dozen decisions is printing noise with a decimal point.

        Stratified by horizon, because a decision settled in five minutes and one
        settled in three weeks are not the same measurement.
        """
        return self.decisions.calibration(model, stratum)

    def pending_decisions(self, model: Optional[str] = None,
                          stratum: Optional[str] = None) -> Dict[str, Any]:
        """What the ledger is still holding open, and whether any of it is late.

        An estate that stops settling its own forecasts does not look broken - it
        looks like a ledger that stopped accumulating. The last rate it reported
        stays on the screen. due_but_unrecorded is the number that catches that.
        """
        rows = self.decisions.pending(model=model, stratum=stratum)
        return {"ok": True, "pending": len(rows), "decisions": rows,
                "oldest_overdue_seconds": self.decisions.overdue_seconds()}

    def due_decisions(self, model: Optional[str] = None) -> Dict[str, Any]:
        """Open decisions whose outcome is knowable now, and were not when made."""
        rows = self.decisions.due(model=model)
        return {"ok": True, "due": len(rows), "decisions": rows}

    def discriminability(self, model: Optional[str] = None,
                         stratum: Optional[str] = None) -> Dict[str, Any]:
        """Whether this population of outcomes could show skill at all.

        The question that has to be answered before any rate is quoted. If
        nineteen outcomes in twenty are the same, a constant guess scores 95% and
        a rate printed without this reads as ability.
        """
        return self.decisions.discriminability(model, stratum)

    # ---------------------------------------------------------------- manifest
    def manifest(self) -> Dict[str, Any]:
        return {
            "service": "attest",
            "version": VERSION,
            "description": "Verifiable memory and claim-checking for any AI. A "
                           "verdict here comes from a checker, not a model.",
            "rule": "verified is always a boolean and is never defaulted to true; "
                    "a checker that did not reach a verdict reports "
                    "checker_error=true, which means UNKNOWN and not disproved.",
            "operations": [
                {"name": "capabilities", "endpoint": "/attest/v1/capabilities",
                 "params": {},
                 "description": "What this machine can and cannot actually check, "
                                "measured rather than asserted. Call this first."},
                {"name": "verify", "endpoint": "/attest/v1/verify",
                 "params": {"claim": "the claim", "kind": "math|code|graph",
                            "who": "caller id", "record": "also store the result"},
                 "description": "Put a claim against a real checker. Not a "
                                "language model's opinion."},
                {"name": "remember", "endpoint": "/attest/v1/remember",
                 "params": {"fact": "the finding", "why": "the evidence",
                            "who": "caller id", "verified": "true ONLY if a "
                            "checker passed it", "domain": "optional label"},
                 "description": "Leave a finding for whoever comes next."},
                {"name": "recall", "endpoint": "/attest/v1/recall",
                 "params": {"query": "what to look for", "limit": "default 5",
                            "only_verified": "only checked facts"},
                 "description": "Read findings back; the tier is preserved."},
                {"name": "memcheck", "endpoint": "/attest/v1/memcheck",
                 "params": {"gb": "how much RAM to test", "passes": "passes"},
                 "description": "Test this machine's RAM for reproducible bit "
                                "errors. If it is failing, every verdict here is "
                                "suspect."},
                {"name": "record_decision",
                 "endpoint": "/attest/v1/record_decision",
                 "params": {"question": "what was decided", "answer": "what was chosen",
                            "qtype": "choice|noul|score",
                            "probability": "REQUIRED for noul",
                            "confidence": "optional",
                            "state": "what it was decided from",
                            "model": "which model answered", "who": "caller id",
                            "due_at": "ISO8601 UTC, e.g. 2026-10-02T04:37:00Z. Supplying "
                                      "this makes it a FORECAST: the decision is held "
                                      "open until the world catches up, and a due_at "
                                      "that has already passed is REFUSED.",
                            "stratum": "which horizon this belongs to, so cadences are "
                                       "never pooled",
                            "cohort": "which batch, so correlated decisions are visible",
                            "target": "what the question is about"},
                 "description": "Log a decision with the confidence it was made at. "
                                "It stays UNRESOLVED, and out of every rate, until an "
                                "outcome is recorded. With a due_at it is a forecast "
                                "rather than a retrospective."},
                {"name": "resolve_decision",
                 "endpoint": "/attest/v1/resolve_decision",
                 "params": {"id": "the decision id", "outcome": "what happened",
                            "correct": "true or false - unknown is not an outcome"},
                 "description": "Record what actually happened, once. A decision that "
                                "can be revised after the fact is a retrospective, "
                                "not a prediction."},
                {"name": "calibration", "endpoint": "/attest/v1/calibration",
                 "params": {"model": "optional, filter to one model",
                            "stratum": "optional, filter to one horizon"},
                 "description": "How often was a decision taken at confidence p "
                                "actually right? Per bucket, with a Wilson interval, "
                                "and it refuses to report a rate below 30 resolved "
                                "decisions. Broken out by stratum, because a decision "
                                "settled in five minutes and one settled in three weeks "
                                "are not the same measurement."},
                {"name": "pending_decisions", "endpoint": "/attest/v1/pending_decisions",
                 "params": {"model": "optional", "stratum": "optional"},
                 "description": "What the ledger is still holding open, split into not "
                                "yet knowable, just come due, knowable and STILL "
                                "unrecorded, and never a forecast at all. The third is "
                                "a broken resolver, and an estate that stops settling "
                                "its own forecasts otherwise looks like one that simply "
                                "stopped accumulating."},
                {"name": "due_decisions", "endpoint": "/attest/v1/due_decisions",
                 "params": {"model": "optional"},
                 "description": "Open decisions whose outcome is knowable now and was "
                                "not when they were made. A decision that never named a "
                                "due_at is never selected: sweeping those up would turn "
                                "'nobody ever checked' into a rate."},
                {"name": "discriminability", "endpoint": "/attest/v1/discriminability",
                 "params": {"model": "optional", "stratum": "optional"},
                 "description": "Could this population of outcomes show skill at all? "
                                "If nineteen outcomes in twenty are the same answer, a "
                                "predictor that always gives that answer scores 95% and "
                                "knows nothing. Ask this BEFORE quoting any rate."},
            ],
        }

    # ---------------------------------------------------------------- dispatch
    def dispatch(self, name: str, args: Dict[str, Any]) -> Dict[str, Any]:
        fn = {"capabilities": lambda a: self.capabilities(),
              "verify": lambda a: self.verify(a.get("claim", ""), a.get("kind"),
                                              a.get("who") or "", a.get("record", True)),
              "remember": lambda a: self.remember(a.get("fact", ""), a.get("why", ""),
                                                  a.get("who") or "",
                                                  a.get("verified", False),
                                                  a.get("checker", ""),
                                                  a.get("domain", "")),
              "recall": lambda a: self.recall(a.get("query", ""),
                                              a.get("limit", 5),
                                              a.get("only_verified", False)),
              "memcheck": lambda a: self.memcheck(a.get("gb", 2), a.get("passes", 2)),
              "record_decision": lambda a: self.record_decision(
                  a.get("question", ""), a.get("answer", ""), a.get("qtype", "choice"),
                  a.get("probability"), a.get("confidence"), a.get("state", ""),
                  a.get("model", ""), a.get("who") or "", a.get("alternatives"),
                  a.get("due_at"), a.get("stratum", ""), a.get("cohort", ""),
                  a.get("target", "")),
              "resolve_decision": lambda a: self.resolve_decision(
                  a.get("id", ""), a.get("outcome", ""), a.get("correct")),
              "calibration": lambda a: self.calibration(a.get("model"),
                                                         a.get("stratum")),
              "pending_decisions": lambda a: self.pending_decisions(
                  a.get("model"), a.get("stratum")),
              "due_decisions": lambda a: self.due_decisions(a.get("model")),
              "discriminability": lambda a: self.discriminability(
                  a.get("model"), a.get("stratum")),
              }.get(name)
        if fn is None:
            return {"ok": False, "error": "unknown operation: %s" % name,
                    "available": sorted([
                        "capabilities", "verify", "remember", "recall", "memcheck",
                        "record_decision", "resolve_decision", "calibration",
                        "pending_decisions", "due_decisions", "discriminability"])}
        t0 = time.time()
        try:
            with self._op_lock:
                out = fn(args or {})
            if not isinstance(out, dict):
                out = {"ok": True, "result": out}
            out.setdefault("ok", True)
        except Exception as exc:
            import traceback
            out = {"ok": False, "error": "%s: %s" % (type(exc).__name__, exc),
                   "traceback": traceback.format_exc()[-1200:]}
        out["operation"] = name
        out["seconds"] = round(time.time() - t0, 3)
        out["service"] = "attest"
        return out
