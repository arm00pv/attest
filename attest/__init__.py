"""attest - verifiable memory and claim-checking for any AI.

A language model may PROPOSE a claim. Only a checker may pass one.
"""

from .store import Store
from .verdict import Verdict, normalize
from .service import Service, VERSION
from .checkers import KIND_ALIASES, KINDS

__all__ = ["Store", "Verdict", "normalize", "Service", "VERSION",
           "KIND_ALIASES", "KINDS"]
__version__ = VERSION
