#!/usr/bin/env python3
"""End-to-end test of the MCP HTTP transports, against a REAL MCP client.

stdio is measured by tests/smoke_mcp.py. This covers the other two, which were
implemented and shipped in the README as UNKNOWN - which is the one thing a
project whose whole argument is "do not claim what you have not measured" must
not leave lying in its own artifact.

    python tests/smoke_mcp_transports.py

Requires the optional SDK. Without it this reports UNMEASURED, exit 2.
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

FAILED = []
SKIPPED = []

# The conventional mount points, plus the bare root. Tried in order and the
# working one is reported, rather than pinning a guess and calling a mismatch a
# failure of the server.
CANDIDATES = {
    "streamable-http": ["/mcp", "/"],
    "sse": ["/sse", "/"],
}


def check(name, cond, detail=""):
    print("  %-5s %s%s" % ("PASS" if cond else "FAIL", name,
                           "" if cond else "   " + str(detail)[:300]))
    if not cond:
        FAILED.append(name)


def payload(result):
    sc = getattr(result, "structuredContent", None)
    if isinstance(sc, dict):
        return sc.get("result", sc)
    for item in getattr(result, "content", []) or []:
        text = getattr(item, "text", None)
        if text:
            try:
                return json.loads(text)
            except Exception:
                return {"_raw": text[:300]}
    return {}


def port_open(port, host="127.0.0.1"):
    with socket.socket() as s:
        s.settimeout(0.5)
        return s.connect_ex((host, port)) == 0


async def use_transport(transport, url, label):
    """Connect, and return the facts gathered. Raises on failure."""
    from mcp import ClientSession
    if transport == "streamable-http":
        from mcp.client.streamable_http import streamable_http_client as opener
    else:
        from mcp.client.sse import sse_client as opener

    async with opener(url) as conn:
        read, write = conn[0], conn[1]
        async with ClientSession(read, write) as session:
            await session.initialize()
            tools = await session.list_tools()
            names = sorted(t.name for t in tools.tools)

            caps = None
            for _ in range(60):
                caps = payload(await session.call_tool("capabilities", {}))
                if caps.get("measured"):
                    break
                await asyncio.sleep(1)

            v = payload(await session.call_tool(
                "verify", {"claim": "print(6 * 7)", "kind": "code", "who": label}))
            u = payload(await session.call_tool(
                "verify", {"claim": "import time; time.sleep(60)", "kind": "code",
                           "who": label}))
            r = payload(await session.call_tool(
                "remember", {"fact": "an asserted note over " + transport,
                             "who": label}))
            rec = payload(await session.call_tool(
                "recall", {"query": "asserted note", "only_verified": True}))
            return {"names": names, "caps": caps, "verified": v, "unknown": u,
                    "remember": r, "only_verified": rec}


def exercise(transport):
    port = 8261 if transport == "streamable-http" else 8262
    home = tempfile.mkdtemp(prefix="attest_tr_")
    env = dict(os.environ)
    env["ATTEST_HOME"] = home
    env["ATTEST_DB"] = os.path.join(home, "tr.db")
    env["ATTEST_TOKEN"] = "transport-smoke"
    env["PYTHONPATH"] = ROOT
    proc = subprocess.Popen(
        [sys.executable, "-m", "attest", "mcp", "--transport", transport,
         "--port", str(port)],
        cwd=ROOT, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        text=True)
    try:
        up = False
        for _ in range(120):
            if proc.poll() is not None:
                break
            if port_open(port):
                up = True
                break
            time.sleep(0.5)
        if not up:
            out = ""
            try:
                out = (proc.stdout.read() or "")[:800] if proc.stdout else ""
            except Exception:
                pass
            check("T[%s] the server started and listened" % transport, False,
                  out or "port %d never opened" % port)
            return

        result, last = None, ""
        for path in CANDIDATES[transport]:
            url = "http://127.0.0.1:%d%s" % (port, path)
            try:
                result = asyncio.run(use_transport(transport, url, transport))
                check("T[%s] a real client connected at %s" % (transport, path),
                      True)
                break
            except Exception as exc:
                last = "%s at %s: %s" % (type(exc).__name__, path, exc)
        if result is None:
            check("T[%s] a real client connected" % transport, False, last)
            return

        # Derived from the manifest, not hardcoded. This said "five" long after
        # there were more, and it stayed wrong because these transports only run
        # when the MCP SDK is installed, which it is not by default.
        import tempfile as _tf
        from attest import Service as _Svc
        _svc = _Svc(os.path.join(_tf.mkdtemp(prefix="attest_tr_"), "m.db"))
        expected = sorted(o["name"] for o in _svc.manifest()["operations"])
        check("T[%s] every manifest operation is exposed over the wire" % transport,
              result["names"] == expected,
              {"wire": result["names"], "manifest": expected})
        check("T[%s] capabilities returns a MEASURED result" % transport,
              result["caps"].get("measured") is True
              and result["caps"].get("ok") is True, list(result["caps"])[:8])
        check("T[%s] a claim that holds comes back verified" % transport,
              result["verified"].get("outcome") == "verified",
              result["verified"])
        check("T[%s] a timed-out checker comes back UNKNOWN with checker_error"
              % transport,
              result["unknown"].get("outcome") == "unknown"
              and result["unknown"].get("checker_error") is True,
              result["unknown"])
        check("T[%s] remember defaults to asserted" % transport,
              result["remember"].get("tier") == "asserted", result["remember"])
        check("T[%s] only_verified does not leak the asserted fact" % transport,
              all(x["verified"] for x in result["only_verified"].get("results", [])),
              result["only_verified"])
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except Exception:
            proc.kill()
        shutil.rmtree(home, ignore_errors=True)


def main():
    try:
        import mcp  # noqa: F401
    except Exception:
        print("attest MCP transport smoke: UNMEASURED - the MCP SDK is not "
              "installed.")
        print("  pip install 'attest-mcp[mcp]'")
        return 2
    print("attest MCP transport smoke test (real clients, real HTTP)")
    print("=" * 66)
    for transport in ("streamable-http", "sse"):
        try:
            exercise(transport)
        except Exception as exc:
            import traceback
            traceback.print_exc()
            check("T[%s] completed" % transport, False,
                  "%s: %s" % (type(exc).__name__, exc))
    print("=" * 66)
    if FAILED:
        print("%d transport checks FAILED: %s" % (len(FAILED), ", ".join(FAILED)))
        return 1
    print("all transport checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
