"""Command line. Every mode reports what it could NOT do."""

from __future__ import annotations

import argparse
import json
import sys

from .service import Service, VERSION


def _print(obj) -> None:
    print(json.dumps(obj, indent=2, default=str))


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        prog="attest",
        description="Verifiable memory and claim-checking for any AI. A verdict "
                    "here comes from a checker, not a model.")
    ap.add_argument("--version", action="version", version="attest " + VERSION)
    ap.add_argument("--db", help="path to the store (default ~/.attest/attest.db)")
    sub = ap.add_subparsers(dest="mode")

    p = sub.add_parser("serve", help="run the HTTP JSON API")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=8250)

    p = sub.add_parser("mcp", help="run the MCP server")
    p.add_argument("--transport", default="stdio",
                   choices=["stdio", "streamable-http", "sse"])
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=8251)

    sub.add_parser("capabilities", help="what this machine can actually check")
    sub.add_parser("manifest", help="the operations, with no measurement needed")
    sub.add_parser("token", help="print the bearer token this machine will use")

    p = sub.add_parser("verify", help="check one claim")
    p.add_argument("claim")
    p.add_argument("--kind", default=None)
    p.add_argument("--who", default="cli")
    p.add_argument("--no-record", action="store_true")

    p = sub.add_parser("remember", help="leave a finding")
    p.add_argument("fact")
    p.add_argument("--why", default="")
    p.add_argument("--who", default="cli")
    p.add_argument("--verified", action="store_true",
                   help="ONLY if a checker actually passed it")
    p.add_argument("--domain", default="")

    p = sub.add_parser("recall", help="read findings back")
    p.add_argument("query")
    p.add_argument("--limit", type=int, default=5)
    p.add_argument("--only-verified", action="store_true")

    p = sub.add_parser("memcheck", help="test this machine's RAM")
    p.add_argument("--gb", type=int, default=2)
    p.add_argument("--passes", type=int, default=2)

    a = ap.parse_args(argv)
    if not a.mode:
        ap.print_help()
        return 1

    if a.mode == "token":
        from .http_api import resolve_token
        print(resolve_token())
        return 0

    svc = Service(a.db)

    if a.mode == "serve":
        from .http_api import serve
        serve(a.host, a.port, svc)
        return 0
    if a.mode == "mcp":
        from . import mcp_api
        cls, where = mcp_api.find_sdk()
        if cls is None:
            # NOT a clean exit. The requested interface is unavailable and
            # saying nothing would be read as success.
            print("attest: " + where, file=sys.stderr)
            return 2
        print("attest: MCP SDK at %s" % where, file=sys.stderr)
        mcp_api.serve(a.transport, a.host, a.port, svc)
        return 0
    if a.mode == "capabilities":
        caps = svc.capabilities()
        if caps.get("warming"):
            svc.warm()
            caps = svc.capabilities()
        _print(caps)
        return 0 if caps.get("ok") else 2
    if a.mode == "manifest":
        _print(svc.manifest())
        return 0
    if a.mode == "verify":
        out = svc.verify(a.claim, a.kind, a.who, not a.no_record)
        _print(out)
        if out.get("verified"):
            return 0
        return 1 if not ("checker_error" in out) else 2
    if a.mode == "remember":
        _print(svc.remember(a.fact, a.why, a.who, a.verified, domain=a.domain))
        return 0
    if a.mode == "recall":
        _print(svc.recall(a.query, a.limit, a.only_verified))
        return 0
    if a.mode == "memcheck":
        _print(svc.memcheck(a.gb, a.passes))
        return 0
    ap.print_help()
    return 1


if __name__ == "__main__":
    sys.exit(main())
