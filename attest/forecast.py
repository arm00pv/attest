"""Prospective decisions - the half of the ledger that had never been used.

WHY THIS EXISTS
---------------
decisions.py can hold a decision open: write it down now with a due_at, settle
it later when the world has caught up. That path existed from the first commit
and had never once been taken. Every decision on the estate's ledger was
recorded and resolved inside the same function call, about something that had
already happened.

A retrospective in a forecast's clothing cannot be wrong about the future,
because it is not about the future. It can still produce a flawless calibration
table, and that table will be quoted.

So this is the part that gives the ledger something to be wrong about: a
probability written down while its answer is still unknown, settled afterwards
by a resolver that did not make the prediction.

CALIBRATION IS NOT SKILL
------------------------
A forecaster that always says 0.30 is perfectly calibrated on a population
where 30% of things are true, and is worth nothing. calibration() answers "when
it said 70%, was it right 70% of the time" - a real question, and not the only
one. Ranking forecasters needs a proper scoring rule, so brier() is here too.
A forecaster is only useful if it beats the base rate on BOTH.
"""

from __future__ import annotations

from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from .decisions import now_iso

__all__ = [
    "Item", "Forecaster", "Constant", "BaseRate", "Persistence",
    "record_forecasts", "resolve_due", "brier", "brier_skill_score",
    "head_to_head",
]


class Item:
    """One thing to be predicted, as it stands right now.

    `target` is what the question is about, and it is the join key for history:
    a base rate is only meaningful per target, because "will this unit fail" has
    a different answer for every unit.
    """

    __slots__ = ("target", "question", "context", "stratum", "alternatives")

    def __init__(self, target: str, question: str, context: str = "",
                 stratum: str = "default",
                 alternatives: Optional[Dict[str, Any]] = None):
        self.target = target
        self.question = question
        self.context = context
        self.stratum = stratum
        self.alternatives = alternatives or {}

    def __repr__(self) -> str:
        return "Item(%r, stratum=%r)" % (self.target, self.stratum)


class Forecaster:
    """A named source of probabilities.

    `name` lands in the ledger's model column, which is the only reason two
    forecasters can be compared at all. Two forecasters scoring the same items
    is an experiment; one forecaster scoring items nobody else saw is an
    anecdote with a decimal point.
    """

    name = "forecaster"

    def predict(self, item: Item, history: Optional[Dict[str, List[bool]]] = None
                ) -> Optional[float]:
        """P(this item resolves true). None means "no opinion" and is refused."""
        raise NotImplementedError


class Constant(Forecaster):
    """A control that CAN fail: it claims the same thing about everything.

    If the ledger cannot call this overconfident when this is wrong, the ledger
    is not measuring calibration, and every other number it prints is
    decoration. It is here to be caught.
    """

    def __init__(self, p: float, name: Optional[str] = None):
        self.p = float(p)
        self.name = name or ("constant-%.2f" % float(p))

    def predict(self, item: Item, history=None) -> Optional[float]:
        return self.p


class BaseRate(Forecaster):
    """Predict the historical frequency. The baseline a model has to beat.

    Laplace-smoothed, because a target that has flipped once in forty runs has
    not been shown to flip 2.5% of the time - and finite evidence should never
    buy a claim of exactly 0 or exactly 1.
    """

    name = "base-rate"

    def __init__(self, history: Optional[Dict[str, List[bool]]] = None,
                 default: float = 0.5, floor: float = 0.02, cap: float = 0.98,
                 name: Optional[str] = None):
        self.history = history or {}
        self.default = float(default)
        self.floor = float(floor)
        self.cap = float(cap)
        if name:
            self.name = name

    def rate(self, target: str) -> float:
        h = self.history.get(target) or []
        if not h:
            return self.default
        r = (sum(1 for x in h if x) + 1.0) / (len(h) + 2.0)
        return min(self.cap, max(self.floor, r))

    def predict(self, item: Item, history=None) -> Optional[float]:
        return self.rate(item.target)


class Persistence(Forecaster):
    """Predict that the next measurement equals the last one.

    On telemetry sampled every few minutes this is a genuinely hard baseline,
    and on a stable estate it is very nearly unbeatable. Saying so is the point:
    a model that cannot beat "same as last time" has not earned a place in the
    routing path, however good its prose is.
    """

    name = "persistence"

    def __init__(self, observed: Optional[Dict[str, bool]] = None,
                 noise: float = 0.02, name: Optional[str] = None):
        self.observed = observed or {}
        self.noise = float(noise)
        if name:
            self.name = name

    def predict(self, item: Item, history=None) -> Optional[float]:
        cur = self.observed.get(item.target)
        if cur is None:
            return 0.5
        # Not exactly 0 or 1: the last sample is evidence, not proof, and a
        # forecaster that never admits doubt scores catastrophically the first
        # time it is wrong.
        return (1.0 - self.noise) if cur else self.noise


# --------------------------------------------------------------------- record
def record_forecasts(ledger, items: Sequence[Item], forecasters: Sequence[Forecaster],
                     cohort: str, due_at: str, who: str = "forecaster",
                     history: Optional[Dict[str, List[bool]]] = None
                     ) -> Dict[str, Any]:
    """Write every forecaster's probability for every item, BEFORE the answer.

    due_at must be in the future or the ledger refuses it, which is the whole
    guard: a decision whose outcome is already knowable is not a prediction.

    One cohort is one batch. Items inside a cohort share a cause - a host that
    reboots takes every target on it down together - so a cohort is closer to
    one observation than to len(items) of them. That is recorded so it can be
    said out loud later rather than discovered by someone re-reading the code.
    """
    written, refused = [], []
    for item in items:
        for f in forecasters:
            p = f.predict(item, history)
            if p is None:
                refused.append({"target": item.target, "model": f.name,
                                "why": "no opinion offered"})
                continue
            p = min(1.0, max(0.0, float(p)))
            # The answer must be the point prediction the probability implies. A
            # row that answers "true" while carrying p=0.005 asserts a proposition
            # and denies it in the same breath, and a table of those is not
            # evidence of anything.
            r = ledger.record(
                question=item.question,
                answer="true" if p >= 0.5 else "false", qtype="noul",
                probability=p, confidence=p, state=item.context,
                model=f.name, who=who, alternatives=item.alternatives,
                due_at=due_at, stratum=item.stratum, cohort=cohort,
                target=item.target)
            if r.get("ok"):
                written.append({"id": r["id"], "target": item.target,
                                "model": f.name, "p": round(p, 4)})
            else:
                refused.append({"target": item.target, "model": f.name,
                                "why": r.get("error")})
    return {"ok": True, "cohort": cohort, "due_at": due_at,
            "items": len(items), "forecasters": len(forecasters),
            "recorded": len(written), "refused": refused, "decisions": written}


# -------------------------------------------------------------------- resolve
def resolve_due(ledger, settle, now: Optional[str] = None,
                model: Optional[str] = None) -> Dict[str, Any]:
    """Settle every decision whose outcome is knowable.

    `settle(decision) -> None | (outcome_text, correct)`. Returning None means
    "not knowable yet" and leaves the decision OPEN, which is the only honest
    option available: a resolver that guesses to keep the numbers moving is
    worse than one that leaves a visible gap.

    A resolver that raises is also left open. An exception is a failure to
    measure, and this project does not launder those into outcomes.
    """
    now = now or now_iso()
    settled, still_open, refused = [], [], []
    for d in ledger.due(now=now, model=model):
        try:
            got = settle(d)
        except Exception as exc:  # noqa: BLE001 - a resolver fault is not an outcome
            still_open.append({"id": d["id"], "target": d.get("target", ""),
                               "why": "resolver raised: %s: %s"
                                      % (type(exc).__name__, exc)})
            continue
        if got is None:
            still_open.append({"id": d["id"], "target": d.get("target", ""),
                               "why": "outcome not knowable from this vantage"})
            continue
        outcome, correct = got
        r = ledger.resolve(d["id"], outcome=str(outcome), correct=bool(correct))
        if r.get("ok"):
            settled.append({"id": d["id"], "target": d.get("target", ""),
                            "model": d.get("model", ""), "p": d.get("probability"),
                            "correct": bool(correct), "outcome": str(outcome)})
        else:
            refused.append({"id": d["id"], "why": r.get("error")})
    return {"ok": True, "at": now, "due": len(settled) + len(still_open) + len(refused),
            "settled": len(settled), "still_open": still_open,
            "refused": refused, "resolutions": settled}


# ---------------------------------------------------------------------- score
def brier(pairs: Iterable[Tuple[float, int]]) -> Optional[float]:
    """Mean squared error of the probabilities. Lower is better.

    The proper scoring rule this ledger was missing. Calibration alone cannot
    rank forecasters - the always-0.30 forecaster is perfectly calibrated on a
    30% population and carries no information whatsoever.
    """
    ps = [(float(p), int(y)) for p, y in pairs if p is not None]
    if not ps:
        return None
    return sum((p - y) ** 2 for p, y in ps) / len(ps)


def brier_skill_score(pairs: Iterable[Tuple[float, int]],
                      reference: float) -> Optional[float]:
    """1 - Brier/Brier_of_the_reference. Positive means it beat the reference.

    Negative is a real and common answer, and it is the one worth printing.
    """
    ps = [(float(p), int(y)) for p, y in pairs if p is not None]
    if not ps or reference <= 0:
        return None
    b = sum((p - y) ** 2 for p, y in ps) / len(ps)
    return 1.0 - (b / reference)


def head_to_head(ledger, models: Sequence[str], stratum: Optional[str] = None,
                 min_n: int = 30) -> Dict[str, Any]:
    """Every named forecaster on the same items, judged the same way.

    Reports calibration, Brier, and whether the population could have shown
    skill at all. The last one is first in the verdict on purpose: a win on a
    population where a constant predictor already scores 97% is not a win.
    """
    rows: List[Dict[str, Any]] = []
    for m in models:
        cal = ledger.calibration(model=m, stratum=stratum)
        dis = ledger.discriminability(model=m, stratum=stratum)
        pairs = ledger.pairs(model=m, stratum=stratum)
        b = brier(pairs)
        # The forecaster's own hit rate on its thresholded prediction. This is not
        # the base rate: an earlier version of this line reported the fraction of
        # propositions that were true and called it accuracy, which is a different
        # number that happens to look right on a balanced population.
        called = sum(1 for p, y in pairs if (1 if p >= 0.5 else 0) == y)
        base = dis.get("base_rate")
        ref = None if base is None else sum(
            (float(base) - y) ** 2 for _p, y in pairs) / len(pairs) if pairs else None
        rows.append({
            "model": m,
            "resolved": cal["resolved"], "unresolved": cal["unresolved"],
            "hit_rate": (None if not pairs else round(called / len(pairs), 4)),
            "base_rate": base,
            "brier": (None if b is None else round(b, 4)),
            "brier_of_base_rate": (None if ref is None else round(ref, 4)),
            "skill_vs_base_rate": (None if (b is None or not ref)
                                   else round(1.0 - b / ref, 4)),
            "can_demonstrate_skill": dis.get("can_demonstrate_skill"),
            "discriminability": dis.get("verdict"),
        })
    rows.sort(key=lambda r: (r["brier"] is None, r["brier"] if r["brier"] is not None else 9e9))
    enough = [r for r in rows if (r["resolved"] or 0) >= min_n]
    if not enough:
        verdict = ("INSUFFICIENT DATA - no forecaster has %d resolved decisions yet. "
                   "Nothing here can be ranked." % min_n)
    elif any(r["can_demonstrate_skill"] is False for r in enough):
        verdict = ("The population cannot demonstrate skill for at least one "
                   "forecaster - a constant guess already scores most of it. "
                   "Ranking these is not meaningful yet.")
    else:
        best = enough[0]
        verdict = ("Best Brier: %s at %s over %d resolved decisions."
                   % (best["model"], best["brier"], best["resolved"]))
    return {"ok": True, "stratum": stratum or "(all)", "min_n": min_n,
            "forecasters": rows, "verdict": verdict}
