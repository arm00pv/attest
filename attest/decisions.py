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

import calendar
import json
import math
import time
import uuid
from typing import Any, Dict, Optional, Tuple

def _iso_to_epoch(ts: str) -> float:
    return calendar.timegm(time.strptime(ts, "%Y-%m-%dT%H:%M:%SZ"))


def now_iso(offset_s: float = 0.0) -> str:
    """UTC timestamp in the one format this ledger uses everywhere.

    Fixed width, so two of them compare correctly as strings and a naive
    comparison cannot silently reorder them.
    """
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() + offset_s))


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
               alternatives: Optional[Dict[str, Any]] = None,
               due_at: Optional[str] = None, stratum: str = "",
               cohort: str = "", target: str = "") -> Dict[str, Any]:
        """Log one decision, with the confidence it was made at.

        The probability is REQUIRED for a noul question. A boolean judgment with
        no probability is exactly the unmeasured claim this ledger exists to
        catch, and it is refused rather than stored as if it were fine.

        due_at IS WHAT MAKES IT A FORECAST. Without it the decision is a
        retrospective: it is scored in the same breath as it is recorded, about
        something that has already happened. With it, the decision is held OPEN
        until the world catches up. The two are not the same object and are not
        scored as if they were - a ledger full of retrospectives can report a
        beautiful calibration while never once having been wrong about the
        future, because it was never about the future.

        A due_at that has already passed is refused. An outcome that is knowable
        at the moment of recording is not a prediction, and letting it in under
        a forecast's name is the one way this table could lie.
        """
        qtype = (qtype or "").strip().lower()
        if qtype not in ("choice", "noul", "score"):
            return {"ok": False, "error": "unknown qtype: %r" % qtype,
                    "accepted": ["choice", "noul", "score"]}
        if qtype == "noul" and probability is None:
            return {"ok": False,
                    "error": "a noul decision MUST carry its probability; a "
                             "judgment with no confidence cannot be scored later"}
        recorded_at = now_iso()
        if due_at is not None:
            due_at = str(due_at).strip()
            if len(due_at) != 20 or due_at[-1] != "Z":
                return {"ok": False,
                        "error": "due_at must look like 2026-10-02T04:37:00Z, "
                                 "got %r" % due_at}
            if due_at <= recorded_at:
                return {"ok": False,
                        "error": "due_at %s is not in the future (now %s). A "
                                 "decision whose outcome is already knowable is "
                                 "a retrospective - record it without due_at."
                                 % (due_at, recorded_at),
                        "prospective": False}
        did = "dec:" + uuid.uuid4().hex[:12]
        self.db.execute(
            "INSERT INTO decisions (id, at, who, model, state, question, qtype,"
            " answer, probability, confidence, alternatives, outcome, correct,"
            " resolved_at, due_at, stratum, cohort, target)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,NULL,NULL,NULL,?,?,?,?)",
            (did, recorded_at, who[:120],
             model[:120], state[:4000], question[:500], qtype, str(answer)[:300],
             (None if probability is None else float(probability)),
             (None if confidence is None else float(confidence)),
             json.dumps(alternatives or {})[:4000],
             due_at, stratum[:60], cohort[:80], target[:200]))
        self.db.commit()
        self.store.ledger("decision_recorded", "%s %s" % (did, qtype))
        return {"ok": True, "id": did, "status": "UNRESOLVED",
                "prospective": due_at is not None,
                "due_at": due_at,
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

    # ------------------------------------------------------------ prospective
    def pending(self, model: Optional[str] = None,
                stratum: Optional[str] = None) -> List[Dict[str, Any]]:
        """Every decision that has been made and not yet settled.

        Returned whether or not it is due. An open decision is a promise the
        ledger is holding, and how many it is holding belongs on the surface.
        """
        q = ("SELECT id, at, who, model, state, question, qtype, answer,"
             " probability, confidence, due_at, stratum, cohort, target"
             " FROM decisions WHERE correct IS NULL")
        args: Tuple[Any, ...] = ()
        if model:
            q += " AND model = ?"
            args += (model,)
        if stratum:
            q += " AND stratum = ?"
            args += (stratum,)
        q += " ORDER BY due_at IS NULL, due_at, at"
        return [dict(r) for r in self.db.execute(q, args).fetchall()]

    def due(self, now: Optional[str] = None,
            model: Optional[str] = None) -> List[Dict[str, Any]]:
        """Open decisions whose outcome is knowable now.

        ONLY decisions that named a due_at can come due. A retrospective that
        was never resolved has no due_at and is never selected: it stays
        unresolved forever, which is the truthful state for it. Sweeping those
        up would quietly convert "nobody ever checked" into a rate.
        """
        now = now or now_iso()
        q = ("SELECT id, at, who, model, state, question, qtype, answer,"
             " probability, confidence, due_at, stratum, cohort, target"
             " FROM decisions WHERE correct IS NULL AND due_at IS NOT NULL"
             " AND due_at <= ?")
        args: Tuple[Any, ...] = (now,)
        if model:
            q += " AND model = ?"
            args += (model,)
        q += " ORDER BY due_at, at"
        return [dict(r) for r in self.db.execute(q, args).fetchall()]

    def overdue_seconds(self, now: Optional[str] = None) -> Optional[float]:
        """How long the oldest already-due, still-unsettled decision has waited.

        The failure this exists to catch: an estate that stops resolving its own
        forecasts does not look broken. It looks like a ledger that has stopped
        accumulating. The last rate it reported stays on the screen, and nobody
        notices the instrument went quiet.
        """
        now = now or now_iso()
        row = self.db.execute(
            "SELECT MIN(due_at) AS m FROM decisions WHERE correct IS NULL"
            " AND due_at IS NOT NULL AND due_at <= ?", (now,)).fetchone()
        if row is None or row["m"] is None:
            return None
        return _iso_to_epoch(now) - _iso_to_epoch(row["m"])

    # ------------------------------------------------------------- calibration
    def calibration(self, model: Optional[str] = None,
                    stratum: Optional[str] = None) -> Dict[str, Any]:
        """How often was a decision taken at confidence p actually right?

        Per bucket, with an interval, and it refuses to be a number when there
        is not enough evidence for one.

        STRATUM IS NOT DECORATION. A decision settled five minutes later and one
        settled three weeks later are not the same measurement, and averaging
        them gives a figure true of neither. When no stratum is named the answer
        is broken out by stratum, and the pooled figure carries that fact.
        """
        where = "correct IS NOT NULL"
        args: list = []
        if model:
            where += " AND model = ?"
            args.append(model)
        if stratum:
            where += " AND stratum = ?"
            args.append(stratum)
        rows = self.db.execute(
            "SELECT probability, correct, stratum, cohort FROM decisions WHERE "
            + where, tuple(args)).fetchall()

        # "Unresolved" is three different situations and collapsing them hides
        # the only one that is a fault. An outcome that is not knowable yet is
        # simply early. One that was knowable and never recorded is a broken
        # instrument. One with no due_at was never a forecast.
        ow = "correct IS NULL"
        oargs: list = []
        if model:
            ow += " AND model = ?"
            oargs.append(model)
        if stratum:
            ow += " AND stratum = ?"
            oargs.append(stratum)
        open_rows = self.db.execute(
            "SELECT due_at FROM decisions WHERE " + ow, tuple(oargs)).fetchall()
        now = now_iso()
        awaiting = sum(1 for r in open_rows if r["due_at"] and r["due_at"] > now)
        missed_due = sum(1 for r in open_rows if r["due_at"] and r["due_at"] <= now)
        never_forecast = sum(1 for r in open_rows if not r["due_at"])
        unresolved = len(open_rows)
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
            overall += (" Of those: %d not yet knowable, %d knowable and STILL "
                        "unrecorded, %d with no due_at at all (never a forecast)."
                        % (awaiting, missed_due, never_forecast))
        if missed_due:
            overall += (" A knowable outcome that was never written down is a "
                        "broken resolver, not a slow one.")

        # Per-stratum, so a horizon cannot hide inside a pooled average.
        strata: Dict[str, Dict[str, Any]] = {}
        for r in rows:
            s = r["stratum"] or "(unstratified)"
            e = strata.setdefault(s, {"n": 0, "hits": 0})
            e["n"] += 1
            e["hits"] += int(r["correct"])
        by_stratum = []
        for s in sorted(strata):
            e = strata[s]
            n, hits = e["n"], e["hits"]
            low, high = wilson(hits, n)
            by_stratum.append({
                "stratum": s, "n": n, "hits": hits,
                "observed_rate": (round(hits / n, 4) if n >= MIN_N else None),
                "interval": ([round(low, 4), round(high, 4)] if n >= MIN_N else None),
                "verdict": ("INSUFFICIENT DATA" if n < MIN_N
                            else "reported"),
            })

        pooled_warning = None
        named = [s["stratum"] for s in by_stratum if s["stratum"] != "(unstratified)"]
        if len(named) > 1 and not stratum:
            pooled_warning = ("This figure POOLS %d different horizons (%s). They "
                              "are not the same measurement; read the per-stratum "
                              "rows instead." % (len(named), ", ".join(sorted(named))))

        # A cohort is one batch, and everything in a batch shares a cause: a host
        # that reboots takes every target on it down together. Sampling every
        # fifteen minutes does not buy fifteen minutes' worth of independent
        # evidence, and an interval computed as if it did is optimistic in a way
        # nobody can see from the number itself.
        cohorts = len({(r["cohort"] or "") for r in rows if r["cohort"]})
        per_cohort = (round(resolved / cohorts, 1) if cohorts else None)
        independence = None
        if cohorts and resolved / cohorts > 5:
            independence = ("%d settled decisions in only %d cohorts (%.1f per "
                            "cohort). Items inside a cohort move together, so this "
                            "interval is OPTIMISTIC - the effective sample is closer "
                            "to %d than to %d."
                            % (resolved, cohorts, resolved / cohorts, cohorts, resolved))

        return {"ok": True, "model": model or "(all)", "stratum": stratum or "(all)",
                "recorded": total, "resolved": resolved, "unresolved": unresolved,
                "cohorts": cohorts, "per_cohort": per_cohort,
                "independence_warning": independence,
                "open": {"awaiting_outcome": awaiting, "due_but_unrecorded": missed_due,
                         "never_a_forecast": never_forecast},
                "min_n": MIN_N, "buckets": out_buckets, "by_stratum": by_stratum,
                "pooled_warning": pooled_warning, "verdict": overall}

    def pairs(self, model: Optional[str] = None,
              stratum: Optional[str] = None) -> List[Tuple[float, int]]:
        """(probability, was_right) for every settled decision.

        Raw material for a proper scoring rule. calibration() alone cannot rank
        forecasters: one that always says 0.30 is perfectly calibrated on a
        population where 30% of things are true, and carries no information at
        all. Ranking needs Brier as well as calibration.
        """
        where = "correct IS NOT NULL AND probability IS NOT NULL"
        args: list = []
        if model:
            where += " AND model = ?"
            args.append(model)
        if stratum:
            where += " AND stratum = ?"
            args.append(stratum)
        return [(float(r["probability"]), int(r["correct"])) for r in self.db.execute(
            "SELECT probability, correct FROM decisions WHERE " + where,
            tuple(args)).fetchall()]

    # -------------------------------------------------------- discriminability
    def discriminability(self, model: Optional[str] = None,
                         stratum: Optional[str] = None) -> Dict[str, Any]:
        """Could this population of outcomes tell a good predictor from a bad one?

        The question that must be answered BEFORE any rate is reported, and the
        one that almost never is. If nineteen outcomes in twenty are the same
        answer, a predictor that always gives that answer scores 95% and knows
        nothing. Printed on its own, that 95% reads as skill.

        So this measures how much the outcomes actually vary, and says plainly
        when the answer is "not enough to demonstrate anything".
        """
        where = "correct IS NOT NULL"
        args: list = []
        if model:
            where += " AND model = ?"
            args.append(model)
        if stratum:
            where += " AND stratum = ?"
            args.append(stratum)
        rows = self.db.execute(
            "SELECT correct FROM decisions WHERE " + where, tuple(args)).fetchall()
        n = len(rows)
        if n == 0:
            return {"ok": True, "n": 0, "base_rate": None, "majority_class": None,
                    "entropy_bits": None, "can_demonstrate_skill": None,
                    "verdict": "NO DATA - nothing has been settled yet"}

        hits = sum(int(r["correct"]) for r in rows)
        base = hits / n
        maj = max(base, 1.0 - base)
        # Shannon entropy in bits: how much a question can possibly tell you.
        # Zero bits means the answer was never in doubt.
        if base <= 0.0 or base >= 1.0:
            bits = 0.0
        else:
            bits = -(base * math.log(base, 2) + (1 - base) * math.log(1 - base, 2))

        if n < MIN_N:
            verdict = ("INSUFFICIENT DATA - n=%d, below the threshold of %d. Whether "
                       "this population can discriminate is not yet known." % (n, MIN_N))
            can: Optional[bool] = None
        elif maj >= 0.95:
            verdict = ("DEGENERATE - a constant predictor that always answers '%s' "
                       "already scores %.1f%%. No amount of skill can show up against "
                       "that. Do not read the rate above as ability."
                       % ("true" if base >= 0.5 else "false", 100.0 * maj))
            can = False
        elif maj >= 0.85:
            verdict = ("WEAK - a constant predictor already scores %.1f%%. Only a "
                       "large and consistent improvement means anything here."
                       % (100.0 * maj))
            can = False
        else:
            verdict = ("DISCRIMINATING - a constant predictor is capped at %.1f%%, so "
                       "skill has room to show. %.3f bits per decision."
                       % (100.0 * maj, bits))
            can = True
        return {"ok": True, "n": n, "hits": hits, "base_rate": round(base, 4),
                "majority_class": round(maj, 4), "entropy_bits": round(bits, 4),
                "can_demonstrate_skill": can, "verdict": verdict}
