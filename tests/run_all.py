#!/usr/bin/env python3
"""Run every suite and report one three-valued verdict.

WHY THIS EXISTS
---------------
The repository held 94 conformance controls and two MCP suites, and nothing ever
ran them automatically. There was no runner and no CI. That is how one of those
controls came to assert "the five operations are exposed as MCP tools" and stay
wrong while there were eleven of them - a control that had silently stopped
meaning anything, in a suite that nobody was executing.

A skipped suite and a passing suite look identical in a summary line. They are not
identical, and this file exists to keep them apart at the process level, in the
same three states the rest of this project uses everywhere else:

    0  every suite ran and passed
    1  a suite ran and FAILED
    2  a suite COULD NOT BE MEASURED - which is not a pass and is not a failure

The MCP suites need an optional dependency. Without it they cannot run, and the
honest report is UNMEASURED, loudly, not a quiet zero.

    python tests/run_all.py
    python tests/run_all.py --install-hint
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)

# (label, script, needs the optional MCP extra)
SUITES = (
    ("conformance", "tests/test_conformance.py", False),
    ("http", "tests/smoke_http.py", False),
    ("mcp-stdio", "tests/smoke_mcp.py", True),
    ("mcp-transports", "tests/smoke_mcp_transports.py", True),
)

PASS, FAIL, UNMEASURED = "PASS", "FAIL", "UNMEASURED"


def run_one(script: str):
    path = os.path.join(ROOT, script)
    try:
        p = subprocess.run([sys.executable, path], cwd=ROOT, capture_output=True,
                           text=True, timeout=900)
    except subprocess.TimeoutExpired:
        return UNMEASURED, "timed out after 900s", ""
    if p.returncode == 0:
        return PASS, "", p.stdout
    if p.returncode == 2:
        return UNMEASURED, "reported that it could not measure", p.stdout
    return FAIL, "exit %d" % p.returncode, (p.stdout or "") + (p.stderr or "")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--quiet", action="store_true", help="only the summary")
    args = ap.parse_args()

    results = []
    for label, script, _optional in SUITES:
        state, why, out = run_one(script)
        results.append((label, state, why, out))

    if not args.quiet:
        for label, state, why, out in results:
            print("=" * 66)
            print("%-16s %s%s" % (label, state, ("  (%s)" % why) if why else ""))
            print("=" * 66)
            if out:
                tail = out.strip().splitlines()
                for line in tail[-6:]:
                    print("  " + line)

    print()
    print("=" * 66)
    width = max(len(r[0]) for r in results)
    for label, state, why, _out in results:
        print("  %-*s  %s%s" % (width, label, state, ("  (%s)" % why) if why else ""))
    print("=" * 66)

    failed = [r[0] for r in results if r[1] == FAIL]
    unmeasured = [r[0] for r in results if r[1] == UNMEASURED]

    if failed:
        print("FAILED: %s" % ", ".join(failed))
    if unmeasured:
        print("NOT MEASURED (these are NOT passes): %s" % ", ".join(unmeasured))
        print("  the MCP suites need the optional extra:")
        print("    pip install 'attest-mcp[mcp]'")

    if failed:
        return 1
    if unmeasured:
        # Deliberately not zero. A run that could not check everything has not
        # established that everything holds, and returning success would say it had.
        return 2
    print("every suite ran and passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
