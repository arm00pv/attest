#!/usr/bin/env python3
"""End-to-end smoke test over the REAL HTTP interface.

Not a unit test. It starts the server as a subprocess, speaks to it with a
plain HTTP client, and checks that the three-valued rule survives the trip out
to a caller - because that trip is where it is easiest to lose.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
PORT = 8299
BASE = "http://127.0.0.1:%d" % PORT
FAILED = []


def check(name, cond, detail=""):
    print("  %-5s %s%s" % ("PASS" if cond else "FAIL", name,
                           "" if cond else "   " + str(detail)[:300]))
    if not cond:
        FAILED.append(name)


def call(path, payload=None, token=None, method=None):
    url = BASE + path
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(url, data=data,
                                 method=method or ("POST" if data else "GET"))
    req.add_header("Content-Type", "application/json")
    if token:
        req.add_header("Authorization", "Bearer " + token)
    try:
        with urllib.request.urlopen(req, timeout=120) as r:
            return r.status, json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read().decode())
        except Exception:
            return e.code, {}


def main():
    home = tempfile.mkdtemp(prefix="attest_smoke_")
    env = dict(os.environ)
    env["ATTEST_HOME"] = home
    env["ATTEST_DB"] = os.path.join(home, "smoke.db")
    env["ATTEST_TOKEN"] = "smoke-token-" + str(int(time.time()))
    env["PYTHONPATH"] = ROOT

    proc = subprocess.Popen([sys.executable, "-m", "attest", "serve", "--port", str(PORT)],
                            cwd=ROOT, env=env, stdout=subprocess.PIPE,
                            stderr=subprocess.STDOUT, text=True)
    try:
        started = False
        for _ in range(60):
            time.sleep(0.5)
            try:
                s, _b = call("/attest/v1/health")
                started = s == 200
                if started:
                    break
            except Exception:
                pass
        if not started:
            print("server did not start")
            print(proc.stdout.read()[:3000] if proc.stdout else "")
            return 1

        s, b = call("/attest/v1/manifest")
        check("S1 the manifest needs no authentication", s == 200, s)
        check("S1b and it states the rule", "never defaulted to true" in
              json.dumps(b.get("rule", "")), b.get("rule"))

        s, b = call("/attest/v1/capabilities")
        check("S2 capabilities WITHOUT a token is refused", s == 401, s)
        s, b = call("/attest/v1/verify", {"claim": "print(1)"})
        check("S2b verify WITHOUT a token is refused", s == 401, s)

        tok = env["ATTEST_TOKEN"]
        # The first call legitimately reports "not measured yet" while the
        # background probe runs, so wait for a real measurement rather than
        # asserting on the warming state.
        b = {}
        for _ in range(60):
            s, b = call("/attest/v1/capabilities", token=tok)
            if b.get("measured"):
                break
            time.sleep(1)
        check("S3 capabilities WITH a token reaches a measured result",
              s == 200 and b.get("measured") is True and b.get("ok") is True, b)
        check("S3b and a not-yet-measured state is never rendered as a failure",
              b.get("verdict") != "" and isinstance(b.get("checks"), dict),
              list(b)[:8])

        s, b = call("/attest/v1/verify",
                    {"claim": "print(2 + 2)", "kind": "code", "who": "smoke"},
                    token=tok)
        check("S4 a claim that holds comes back verified=true",
              b.get("verified") is True and b.get("outcome") == "verified", b)

        s, b = call("/attest/v1/verify",
                    {"claim": "raise SystemExit(4)", "kind": "code", "who": "smoke"},
                    token=tok)
        check("S5 a claim that fails comes back DISPUTED, not unknown",
              b.get("outcome") == "disputed" and "checker_error" not in b, b)

        s, b = call("/attest/v1/verify",
                    {"claim": "import time; time.sleep(60)", "kind": "code",
                     "who": "smoke"}, token=tok)
        check("S6 a checker that timed out comes back UNKNOWN with checker_error",
              b.get("outcome") == "unknown" and b.get("checker_error") is True, b)
        check("S6b and the interpretation tells the caller it is not a disproof",
              "NOT a disproof" in b.get("interpretation", ""),
              b.get("interpretation"))

        s, b = call("/attest/v1/verify", {"claim": "x", "kind": "tarot"}, token=tok)
        check("S7 an unknown kind is refused, not defaulted",
              b.get("ok") is False and "accepted" in b, b)

        s, b = call("/attest/v1/remember",
                    {"fact": "the pump is asserted to be warm", "who": "agent"},
                    token=tok)
        check("S8 remember defaults to ASSERTED",
              b.get("tier") == "asserted", b)

        s, b = call("/attest/v1/recall",
                    {"query": "pump warm", "only_verified": True}, token=tok)
        check("S9 an asserted fact is NOT returned by only_verified",
              all(r["verified"] for r in b.get("results", [])), b)

        s, b = call("/attest/v1/recall", {"query": "pump warm"}, token=tok)
        got = b.get("results", [])
        check("S9b but it IS returned without the filter, carrying tier=asserted",
              got and got[0]["tier"] == "asserted" and got[0]["verified"] is False,
              got)

        s, b = call("/attest/v1/nonsense", {}, token=tok)
        check("S10 an unknown operation is refused and lists the real ones",
              b.get("ok") is False and "available" in b, b)
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except Exception:
            proc.kill()
        shutil.rmtree(home, ignore_errors=True)

    print("")
    if FAILED:
        print("%d smoke checks FAILED: %s" % (len(FAILED), ", ".join(FAILED)))
        return 1
    print("all smoke checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
