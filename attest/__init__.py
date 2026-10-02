"""attest - verifiable memory and claim-checking for any AI.

A language model may PROPOSE a claim. Only a checker may pass one.
"""

from .store import Store
from .verdict import Verdict, normalize
from .service import Service, VERSION
from .checkers import KIND_ALIASES, KINDS
from .decisions import DecisionLedger, now_iso
from .forecast import (Item, Forecaster, Constant, BaseRate, Persistence,
                        record_forecasts, resolve_due, brier,
                        brier_skill_score, head_to_head)

__all__ = ["Store", "Verdict", "normalize", "Service", "VERSION",
           "KIND_ALIASES", "KINDS", "DecisionLedger", "now_iso",
           "Item", "Forecaster", "Constant", "BaseRate", "Persistence",
           "record_forecasts", "resolve_due", "brier", "brier_skill_score",
           "head_to_head"]
__version__ = VERSION
