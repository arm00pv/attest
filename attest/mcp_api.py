"""MCP, so an agent host gets these operations as native tools.

The MCP SDK is NOT a hard dependency. If it is missing this module says so
plainly and the rest of the service still works - which is the same rule as
everything else here: report what you cannot do rather than failing obscurely.
"""

from __future__ import annotations

from typing import Any, Dict, Optional, Tuple

from .service import Service, VERSION

# The SDK has moved between releases. Try the known shapes in order and report
# which one was found, rather than pinning one and breaking silently.
CANDIDATES = (
    ("mcp.server.mcpserver", "MCPServer"),
    ("mcp.server.fastmcp", "FastMCP"),
    ("mcp.server", "Server"),
)


def find_sdk() -> Tuple[Optional[object], str]:
    """Return (class, 'module.Class') or (None, reason)."""
    import importlib
    errors = []
    for mod, attr in CANDIDATES:
        try:
            m = importlib.import_module(mod)
            cls = getattr(m, attr, None)
            if cls is not None:
                return cls, "%s.%s" % (mod, attr)
        except Exception as exc:
            errors.append("%s: %s" % (mod, str(exc)[:60]))
    return None, ("no MCP SDK found. Install it with: pip install 'attest-mcp[mcp]'. "
                  "Tried: " + "; ".join(errors))


def build(svc: Optional[Service] = None, sdk_cls: Any = None):
    svc = svc or Service()
    if sdk_cls is None:
        sdk_cls, where = find_sdk()
        if sdk_cls is None:
            raise SystemExit("attest: " + where)
    else:
        where = "injected"
    try:
        srv = sdk_cls(
            name="attest",
            title="Attest",
            version=VERSION,
            description="Verifiable memory and claim-checking. Verdicts come from "
                        "real checkers (Lean 4, execution, a knowledge graph), "
                        "never from a language model.",
            instructions=(
                "Call capabilities() first to learn what this machine can actually "
                "check. Use verify() when you want a claim checked rather than "
                "believed: it returns a boolean 'verified', and 'checker_error' "
                "means NO VERDICT WAS REACHED - unknown, not disproved. Use "
                "remember() to leave findings, and set verified=true ONLY when a "
                "checker passed it. recall() returns each finding with its tier, "
                "so a checked fact can be told from an asserted one."))
    except TypeError:
        # Older SDKs take a plain name only.
        srv = sdk_cls("attest")

    # The SDK derives each tool's JSON schema from the function SIGNATURE. The
    # operations live in a dict, so registering them directly fails with
    # "InvalidSignature: Parameter _args ... cannot start with '_'". Found by
    # running a real MCP client rather than assuming it worked. Each operation
    # therefore gets a thin wrapper with real, named, typed parameters - which is
    # also what gives the host a usable schema.
    def capabilities() -> Dict[str, Any]:
        """Report what this machine can and cannot actually check, measured
        rather than asserted. Call this FIRST."""
        return svc.dispatch("capabilities", {})

    def verify(claim: str, kind: str = "", who: str = "mcp-client",
               record: bool = True) -> Dict[str, Any]:
        """Put a claim against a real checker and return a verdict - not a
        language model's opinion. kind: 'math', 'code' or 'graph'. A claim that
        cannot be checked comes back verified=false with checker_error=true,
        which means UNKNOWN, never verified by default."""
        return svc.dispatch("verify", {"claim": claim, "kind": kind or None,
                                       "who": who, "record": record})

    def remember(fact: str, why: str = "", who: str = "mcp-client",
                 verified: bool = False, domain: str = "") -> Dict[str, Any]:
        """Leave a finding for whoever comes next. Set verified=true ONLY if a
        checker actually passed it."""
        return svc.dispatch("remember", {"fact": fact, "why": why, "who": who,
                                         "verified": verified, "domain": domain})

    def recall(query: str, limit: int = 5, only_verified: bool = False) -> Dict[str, Any]:
        """Read findings back. Every result carries its tier, so a checked fact
        can be told from an asserted one."""
        return svc.dispatch("recall", {"query": query, "limit": limit,
                                       "only_verified": only_verified})

    def memcheck(gb: int = 2, passes: int = 2) -> Dict[str, Any]:
        """Test this machine's RAM for reproducible bit errors, right now. If the
        memory is failing, every verdict here is suspect."""
        return svc.dispatch("memcheck", {"gb": gb, "passes": passes})

    def record_decision(question: str, answer: str, qtype: str = "choice",
                        probability: float = -1.0, confidence: float = -1.0,
                        state: str = "", model: str = "",
                        who: str = "mcp-client") -> Dict[str, Any]:
        """Log a decision WITH the confidence it was made at. probability is
        REQUIRED for a noul judgment. It stays UNRESOLVED, and out of every rate,
        until an outcome is recorded - an unsettled decision is not a correct one."""
        return svc.dispatch("record_decision", {
            "question": question, "answer": answer, "qtype": qtype,
            "probability": None if probability < 0 else probability,
            "confidence": None if confidence < 0 else confidence,
            "state": state, "model": model, "who": who})

    def resolve_decision(id: str, outcome: str, correct: bool) -> Dict[str, Any]:
        """Record what actually happened, ONCE. 'I do not know how it turned out'
        is not an outcome; leave it unresolved instead."""
        return svc.dispatch("resolve_decision", {"id": id, "outcome": outcome,
                                                 "correct": correct})

    def calibration(model: str = "") -> Dict[str, Any]:
        """How often was a decision taken at confidence p actually right? Per
        bucket with a Wilson interval, refusing to report a rate below 30
        resolved decisions - at 108 items a few points of difference is noise."""
        return svc.dispatch("calibration", {"model": model or None})

    _register(srv, "capabilities", capabilities)
    _register(srv, "verify", verify)
    _register(srv, "remember", remember)
    _register(srv, "recall", recall)
    _register(srv, "memcheck", memcheck)
    _register(srv, "record_decision", record_decision)
    _register(srv, "resolve_decision", resolve_decision)
    _register(srv, "calibration", calibration)
    srv._attest_sdk = where
    return srv


def _register(srv: Any, name: str, fn: Any) -> None:
    tool = getattr(srv, "tool", None)
    if callable(tool):
        tool(name=name, description=(fn.__doc__ or "").strip())(fn)
        return
    raise SystemExit("attest: the MCP SDK object has no .tool() registrar - "
                     "unsupported SDK version")


def serve(transport: str = "stdio", host: str = "127.0.0.1", port: int = 8251,
          svc: Optional[Service] = None) -> None:
    srv = build(svc)
    if transport == "stdio":
        srv.run(transport="stdio")
    elif transport in ("streamable-http", "http"):
        srv.run(transport="streamable-http", host=host, port=port)
    elif transport == "sse":
        srv.run(transport="sse", host=host, port=port)
    else:
        raise SystemExit("unknown transport: %s" % transport)
