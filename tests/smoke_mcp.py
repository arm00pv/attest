#!/usr/bin/env python3
"""End-to-end test of the MCP interface, against a REAL MCP client.

Not a schema inspection. It launches the server as a subprocess, speaks actual
MCP over stdio, and checks that the three-valued rule survives that trip - which
is exactly where it is easiest to lose, because a host usually renders whatever
the tool returns straight to a model.

Requires the optional SDK:  pip install "attest-mcp[mcp]"
Without it this reports UNMEASURED, exit 2. It does NOT report success.
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

FAILED = []


def check(name, cond, detail=""):
    print("  %-5s %s%s" % ("PASS" if cond else "FAIL", name,
                           "" if cond else "   " + str(detail)[:300]))
    if not cond:
        FAILED.append(name)


def payload(result):
    """Pull the JSON body out of an MCP tool result."""
    try:
        sc = getattr(result, "structuredContent", None)
        if isinstance(sc, dict):
            return sc.get("result", sc)
    except Exception:
        pass
    for item in getattr(result, "content", []) or []:
        text = getattr(item, "text", None)
        if text:
            try:
                return json.loads(text)
            except Exception:
                return {"_raw": text[:400]}
    return {}


async def run():
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    home = tempfile.mkdtemp(prefix="attest_mcp_")
    env = dict(os.environ)
    env["ATTEST_HOME"] = home
    env["ATTEST_DB"] = os.path.join(home, "mcp.db")
    env["ATTEST_TOKEN"] = "mcp-smoke-token"
    env["PYTHONPATH"] = ROOT
    params = StdioServerParameters(command=sys.executable,
                                   args=["-m", "attest", "mcp"], env=env)

    try:
        async with stdio_client(params) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()

                tools = await session.list_tools()
                names = sorted(t.name for t in tools.tools)
                check("M1 the five operations are exposed as MCP tools",
                      names == ["capabilities", "memcheck", "recall",
                                "remember", "verify"], names)

                desc = {t.name: (t.description or "") for t in tools.tools}
                check("M2 the verify tool description states the rule, so a host "
                      "cannot read UNKNOWN as false",
                      "checker_error" in desc.get("verify", "")
                      and "UNKNOWN" in desc.get("verify", ""),
                      desc.get("verify", "")[:200])
                check("M2b and remember tells the caller when verified=true is legal",
                      "ONLY" in desc.get("remember", ""), desc.get("remember", "")[:200])

                caps = None
                for _ in range(60):
                    caps = payload(await session.call_tool("capabilities", {}))
                    if caps.get("measured"):
                        break
                    await asyncio.sleep(1)
                check("M3 capabilities returns a MEASURED result over MCP",
                      caps.get("measured") is True and caps.get("ok") is True,
                      list(caps)[:8])
                check("M3b with a verdict naming what can be checked",
                      "can check:" in caps.get("verdict", ""), caps.get("verdict"))

                v = payload(await session.call_tool(
                    "verify", {"claim": "print(2 + 2)", "kind": "code",
                               "who": "mcp-smoke"}))
                check("M4 a claim that holds comes back verified",
                      v.get("outcome") == "verified", v)

                d = payload(await session.call_tool(
                    "verify", {"claim": "raise SystemExit(7)", "kind": "code",
                               "who": "mcp-smoke"}))
                check("M5 a claim that fails comes back DISPUTED",
                      d.get("outcome") == "disputed" and "checker_error" not in d, d)

                u = payload(await session.call_tool(
                    "verify", {"claim": "import time; time.sleep(60)",
                               "kind": "code", "who": "mcp-smoke"}))
                check("M6 a checker that timed out comes back UNKNOWN with "
                      "checker_error, NOT as a disproof",
                      u.get("outcome") == "unknown" and u.get("checker_error") is True,
                      u)

                r = payload(await session.call_tool(
                    "remember", {"fact": "the door is asserted to be shut",
                                 "who": "mcp-smoke"}))
                check("M7 remember defaults to ASSERTED", r.get("tier") == "asserted", r)

                only = payload(await session.call_tool(
                    "recall", {"query": "door shut", "only_verified": True}))
                check("M8 only_verified does not return the asserted fact",
                      all(x["verified"] for x in only.get("results", [])), only)

                allf = payload(await session.call_tool(
                    "recall", {"query": "door shut"}))
                got = allf.get("results", [])
                check("M8b unfiltered it comes back carrying tier=asserted",
                      got and got[0]["tier"] == "asserted", got)
    finally:
        shutil.rmtree(home, ignore_errors=True)


def main():
    try:
        import mcp  # noqa: F401
    except Exception:
        print("attest MCP smoke: UNMEASURED - the MCP SDK is not installed.")
        print("  pip install 'attest-mcp[mcp]'")
        return 2
    print("attest MCP smoke test (real client, real stdio)")
    print("=" * 66)
    try:
        asyncio.run(run())
    except Exception as exc:
        import traceback
        traceback.print_exc()
        check("M0 the MCP session completed", False, "%s: %s" % (type(exc).__name__, exc))
    print("=" * 66)
    if FAILED:
        print("%d MCP checks FAILED: %s" % (len(FAILED), ", ".join(FAILED)))
        return 1
    print("all MCP checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
