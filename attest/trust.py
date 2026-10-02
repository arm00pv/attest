"""Fleet trust - which host should this actually be run on?

WHY THIS EXISTS
---------------
An estate with more than one machine makes a routing decision every time it runs
anything, and almost always makes it from folklore: this box is the fast one, that
box is the flaky one, the small one can be trusted with small things. Folklore is
an assertion. Nothing counts it.

The estate this was extracted from already holds 98 assertions about its three hosts
and re-verifies every one of them four times a day. That is a real, regular, measured
panel. What it answers is "is this true NOW?". What routing needs is "will this still
be true when the work is running?", and nothing was measuring that at all.

So a host's trustworthiness here is not a property someone assigns. It is a
probability, recorded before the next check and settled after it, in the same ledger
everything else in this project is settled in.

THE BASELINE THAT MATTERS
------------------------
AlwaysHolds - "it is true now, so it will be true later" - is what every casual claim
that a host is fine is actually asserting. It is strong on a stable estate and it is
the number a real trust model has to beat. On short horizons it is nearly unbeatable;
that is a fact about the estate, not a weakness of the model, and saying so is the
point.

WHAT ROUTING IS ALLOWED TO CLAIM
--------------------------------
route() picks a host and states the measured probability that the chosen host's
assertions hold over the window the work needs. The choice is itself written to the
ledger as a forecast, so a router that routes badly is a router whose calibration
shows it. A routing policy that cannot be wrong cannot be trusted.
"""

from __future__ import annotations

from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from .decisions import now_iso
from .forecast import Forecaster, Item, brier_decomposition

__all__ = ["Assertion", "AlwaysHolds", "FlipRate", "HostStability",
           "assertions_to_items", "host_stability", "route"]


class Assertion:
    """One thing the estate believes about a host, right now.

    `observed` is the measured value, not a belief about it. An assertion whose
    value could not be measured has no business being here: it is passed in as
    None and dropped, because a host that could not be checked and a host that
    checked out clean are different facts and the difference is the whole point.
    """

    __slots__ = ("host", "key", "kind", "observed", "note")

    def __init__(self, host: str, key: str, kind: str, observed: Optional[bool],
                 note: str = ""):
        self.host = host
        self.key = key
        self.kind = kind
        self.observed = observed
        self.note = note

    def item(self, horizon_minutes: int) -> Item:
        return Item(
            target="%s|%s" % (self.host, self.key),
            question="will %s still hold on %s at the next check (%d min)?"
                     % (self.key, self.host, horizon_minutes),
            context="kind=%s observed=%s %s"
                    % (self.kind, self.observed, self.note[:160]),
            stratum="hosttrust",
            alternatives={"host": self.host, "key": self.key, "kind": self.kind})

    def __repr__(self) -> str:
        return "Assertion(%s, %s, observed=%s)" % (self.host, self.kind,
                                                   self.observed)


def assertions_to_items(assertions: Iterable[Assertion],
                        horizon_minutes: int) -> List[Item]:
    """Drop anything unmeasurable. An unchecked assertion is not a false one."""
    return [a.item(horizon_minutes) for a in assertions if a.observed is not None]


class AlwaysHolds(Forecaster):
    """The naive baseline: true now means true later.

    This is the assumption underneath every casual statement that a host is fine,
    so it is the number any real trust model has to beat rather than a straw man.
    """

    name = "always-holds"

    def __init__(self, observed: Dict[str, bool], epsilon: float = 0.01,
                 name: Optional[str] = None):
        self.observed = observed
        self.epsilon = float(epsilon)
        if name:
            self.name = name

    def predict(self, item: Item, history=None) -> Optional[float]:
        cur = self.observed.get(item.target)
        if cur is None:
            return None
        # Never exactly 0 or 1: the last check is evidence, not proof.
        return (1.0 - self.epsilon) if cur else self.epsilon


class FlipRate(Forecaster):
    """How often has THIS assertion changed, historically. Laplace-smoothed.

    Per key rather than per host, because "the disk is not full" and "the service
    is up" do not fail at the same rate, and averaging them into one host number
    throws away the only thing that makes a trust estimate worth having.
    """

    name = "flip-rate"

    def __init__(self, history: Dict[str, List[bool]], observed: Dict[str, bool],
                 floor: float = 0.005, cap: float = 0.995,
                 name: Optional[str] = None):
        self.history = history or {}
        self.observed = observed or {}
        self.floor = float(floor)
        self.cap = float(cap)
        if name:
            self.name = name

    def predict(self, item: Item, history=None) -> Optional[float]:
        cur = self.observed.get(item.target)
        if cur is None:
            return None
        h = self.history.get(item.target) or []
        if len(h) < 2:
            # Not enough of its own history: fall back to the naive assumption
            # rather than inventing a rate from one observation.
            return 0.99 if cur else 0.01
        flips = sum(1 for a, b in zip(h, h[1:]) if a != b)
        # P(holds) estimated from the observed flip rate, smoothed so a short
        # clean record does not buy a claim of certainty.
        p_flip = (flips + 1.0) / (len(h) - 1 + 2.0)
        p = (1.0 - p_flip) if cur else p_flip
        return min(self.cap, max(self.floor, p))


class HostStability(Forecaster):
    """How often have ANY of this host's assertions changed, pooled.

    This is the trust signal, stated as a probability rather than a reputation.
    Pooling across a host's assertions is deliberate here, unlike FlipRate: the
    question being asked is about the host, and a host that is failing tends to
    fail in several places at once.
    """

    name = "host-stability"

    def __init__(self, history: Dict[str, List[bool]],
                 observed: Dict[str, bool], host_of: Dict[str, str],
                 floor: float = 0.005, cap: float = 0.995,
                 name: Optional[str] = None):
        self.history = history or {}
        self.observed = observed or {}
        self.host_of = host_of or {}
        self.floor = float(floor)
        self.cap = float(cap)
        if name:
            self.name = name

    def host_flip_rate(self, host: str) -> Tuple[float, int]:
        flips = pairs = 0
        for target, h in self.history.items():
            if self.host_of.get(target) != host or len(h) < 2:
                continue
            flips += sum(1 for a, b in zip(h, h[1:]) if a != b)
            pairs += len(h) - 1
        if pairs == 0:
            return (0.0, 0)
        return ((flips + 1.0) / (pairs + 2.0), pairs)

    def predict(self, item: Item, history=None) -> Optional[float]:
        cur = self.observed.get(item.target)
        if cur is None:
            return None
        host = self.host_of.get(item.target)
        if host is None:
            return 0.99 if cur else 0.01
        p_flip, pairs = self.host_flip_rate(host)
        if pairs == 0:
            return 0.99 if cur else 0.01
        p = (1.0 - p_flip) if cur else p_flip
        return min(self.cap, max(self.floor, p))


def host_stability(history: Dict[str, List[bool]], host_of: Dict[str, str]
                   ) -> Dict[str, Dict[str, Any]]:
    """Per-host measured stability, with the evidence behind it.

    Reported with the number of observation pairs it rests on, because a host with
    a perfect record over four checks and a host with a perfect record over four
    hundred are not the same claim and must not print the same number.
    """
    out: Dict[str, Dict[str, Any]] = {}
    hosts = sorted({h for h in host_of.values()})
    for host in hosts:
        flips = pairs = keys = 0
        lengths: List[int] = []
        for target, h in history.items():
            if host_of.get(target) != host:
                continue
            keys += 1
            lengths.append(len(h))
            if len(h) < 2:
                continue
            flips += sum(1 for a, b in zip(h, h[1:]) if a != b)
            pairs += len(h) - 1
        rate = None if pairs == 0 else (flips + 1.0) / (pairs + 2.0)
        # OBSERVATION PAIRS ARE NOT OBSERVATION STEPS, and this is the same trap as
        # many decisions inside one cohort. Sixty-one assertions checked twice is
        # sixty-one pairs and exactly ONE transition - enough to make a flip rate
        # look well-founded while it rests on a single moment in time.
        out[host] = {"assertions": keys, "observation_pairs": pairs,
                     "checks": (min(lengths) - 1 if lengths else 0),
                     "flips": flips, "flip_rate": (None if rate is None
                                                   else round(rate, 5)),
                     "stable_probability": (None if rate is None
                                             else round(1.0 - rate, 5))}
    return out


def route(hosts: Sequence[str], stability: Dict[str, Dict[str, Any]],
          require_pairs: int = 4, require_checks: int = 3) -> Dict[str, Any]:
    """Pick a host, and say only what has actually been measured about the choice.

    A host with too little evidence is not ranked behind the others - it is
    EXCLUDED, and named as excluded. Quietly ordering an unmeasured host last is
    how an estate ends up believing it has compared three machines when it has
    compared two.

    Both thresholds are enforced. A host is not rankable on pairs of observations
    alone: a hundred assertions sampled twice yield a hundred pairs and one moment
    in time, and a stability figure resting on that looks exactly like one resting
    on a week of evidence.
    """
    able, excluded = [], []
    for h in hosts:
        s = stability.get(h) or {}
        checks = s.get("checks") or 0
        pairs = s.get("observation_pairs") or 0
        if checks < require_checks:
            excluded.append({"host": h, "why": "only %d check(s), %d needed to rank"
                                               % (checks, require_checks)})
            continue
        if pairs < require_pairs:
            excluded.append({"host": h, "why": "only %d observation pair(s), %d "
                                               "needed to rank" % (pairs,
                                                                   require_pairs)})
            continue
        able.append((h, s))

    if not able:
        return {"ok": True, "chosen": None, "ranked": [], "excluded": excluded,
                "verdict": "NO HOST HAS ENOUGH EVIDENCE TO BE CHOSEN. This is not a "
                           "tie and it is not a failure; it is the honest state of "
                           "not knowing, and routing on it would be routing on "
                           "folklore."}

    able.sort(key=lambda t: (-(t[1].get("stable_probability") or 0.0),
                             -(t[1].get("observation_pairs") or 0)))
    best, bs = able[0]
    ranked = [{"host": h, "stable_probability": s.get("stable_probability"),
               "observation_pairs": s.get("observation_pairs"),
               "checks": s.get("checks"), "flip_rate": s.get("flip_rate")}
              for h, s in able]
    runner = ranked[1] if len(ranked) > 1 else None
    verdict = ("%s, at %.4f measured stability over %d check(s) and %d "
               "observation pair(s)."
               % (best, bs.get("stable_probability") or 0.0,
                  bs.get("checks") or 0, bs.get("observation_pairs") or 0))
    if runner is not None:
        gap = (bs.get("stable_probability") or 0.0) -               (runner["stable_probability"] or 0.0)
        verdict += (" Next is %s at %.4f, a gap of %.4f."
                    % (runner["host"], runner["stable_probability"] or 0.0, gap))
        if abs(gap) < 0.01:
            verdict += (" That gap is inside the noise at this sample size: the "
                        "order between these two is not established.")
    return {"ok": True, "chosen": best, "ranked": ranked, "excluded": excluded,
            "verdict": verdict}
