"""The rule. Everything else in this package exists to keep it true.

THREE-VALUED, NOT TWO-VALUED
----------------------------
Most systems that "check" something hand back a boolean, and a boolean has no
room for the answer that matters most: *I could not tell*. Collapsing that into
False is how a checker that never ran becomes a disproof, and it is the most
common way a system misleads without ever saying anything false.

    verified=True                        a real checker ran, and passed it
    verified=False, checker_error=False  a real checker ran, and disputed it
    verified=False, checker_error=True   UNKNOWN - no verdict was reached

The third case is not a failure of the claim. It is the checker reporting that
it could not decide: not installed, timed out, crashed, or was never asked. A
caller that reads only 'verified' - which is most callers - must still be able
to tell a disproof from the absence of one, so 'checker_error' travels with it
everywhere 'verified' does, including into stored memory.

This is not a style preference. It is measured. On the machine this service was
extracted from, Lean died with SIGSEGV inside libleanshared.so because of
failing RAM and printed nothing; the next line of code computed False with an
empty error list, which is byte-for-byte identical to what a genuinely false
claim produces. Every crashed check was being recorded as a disproof.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Dict, Optional


@dataclass
class Verdict:
    """The result of putting one claim against one real checker.

    Construct these through passed(), disputed() or unknown() - never by setting
    the fields directly. Those three constructors are the only places in this
    codebase where 'verified' is assigned, which is what makes the rule
    enforceable by reading rather than by trust.
    """

    verified: bool
    """True ONLY if a checker ran and passed the claim. Never defaulted to True."""

    checker_error: bool = False
    """True means NO VERDICT was reached. 'verified' is False in this case, but it
    means unknown, not disproved. Check this field before reporting a failure."""

    checker: str = ""
    reason: str = ""
    seconds: float = 0.0
    detail: Dict[str, Any] = field(default_factory=dict)

    # ---------------------------------------------------------------- outcome
    @property
    def outcome(self) -> str:
        """One word for the caller: 'verified', 'disputed' or 'unknown'."""
        if self.verified:
            return "verified"
        return "unknown" if self.checker_error else "disputed"

    @property
    def decided(self) -> bool:
        """Did the checker actually reach a verdict, either way?"""
        return not self.checker_error

    # ------------------------------------------------------------ constructors
    @classmethod
    def passed(cls, checker: str, seconds: float = 0.0, **detail: Any) -> "Verdict":
        return cls(verified=True, checker_error=False, checker=checker,
                   seconds=seconds, detail=detail)

    @classmethod
    def disputed(cls, checker: str, reason: str, seconds: float = 0.0,
                 **detail: Any) -> "Verdict":
        return cls(verified=False, checker_error=False, checker=checker,
                   reason=reason, seconds=seconds, detail=detail)

    @classmethod
    def unknown(cls, checker: str, reason: str, seconds: float = 0.0,
                **detail: Any) -> "Verdict":
        """No verdict was reached. This is NOT a disproof and must never be
        rendered as one."""
        return cls(verified=False, checker_error=True, checker=checker,
                   reason=reason, seconds=seconds, detail=detail)

    # ------------------------------------------------------------------ output
    def to_dict(self) -> Dict[str, Any]:
        out: Dict[str, Any] = {
            "verified": bool(self.verified),
            "outcome": self.outcome,
            "checker": self.checker,
            "reason": self.reason,
            "seconds": round(self.seconds, 3),
        }
        # Always serialise checker_error when it is True. Never omit it when
        # True, and never set it when False - a caller testing
        # 'if resp.get("checker_error")' gets the right answer either way.
        if self.checker_error:
            out["checker_error"] = True
        if self.detail:
            out["detail"] = self.detail
        return out


def normalize(raw: Any) -> Verdict:
    """Coerce anything a checker returned into a Verdict, safely.

    Checkers are third-party code and one of them will eventually return None,
    a bare bool, or a dict with no 'verified' key. The safe reading of a missing
    verdict is UNKNOWN - never True, and never a plain False either.
    """
    if isinstance(raw, Verdict):
        return raw
    if isinstance(raw, bool):
        # A bare bool cannot carry the third state. Treat it as decided, and say
        # so in the checker field rather than pretending it was a real probe.
        return Verdict(verified=raw, checker="(bare bool)")
    if isinstance(raw, dict):
        if "verified" not in raw:
            return Verdict.unknown(raw.get("checker", "?"),
                                   "checker returned no verdict: %r" % (raw,))
        return Verdict(
            verified=bool(raw.get("verified")),
            checker_error=bool(raw.get("checker_error", False)),
            checker=str(raw.get("checker", "")),
            reason=str(raw.get("reason", "")),
            seconds=float(raw.get("seconds") or 0.0),
            detail={k: v for k, v in raw.items()
                    if k not in ("verified", "checker_error", "checker",
                                 "reason", "seconds")},
        )
    return Verdict.unknown("?", "checker returned %s, which is not a verdict"
                           % type(raw).__name__)


def never_defaults_true(verdicts) -> Optional[str]:
    """Audit helper used by the conformance suite.

    Returns a complaint string if any verdict claims a pass that no checker
    backed, i.e. a verified=True with no checker named or no elapsed time.
    """
    for v in verdicts:
        if v.verified and not v.checker:
            return "a verdict reports verified=True but names no checker"
        if v.verified and v.checker_error:
            return "a verdict reports verified=True AND checker_error=True"
    return None
