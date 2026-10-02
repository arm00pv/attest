#!/usr/bin/env python3
"""deploy_check.py - does what is deployed match what is in the repository?

WHY THIS EXISTS
---------------
On 2026-10-02 the same mistake was made three times in one day. Code was changed and
committed, the change was tested, and the running deployment kept the old file:

  * store.py    - WAL and the 30-second busy timeout were added and never deployed, so
                  the live ledger still reported journal_mode=delete and a 5s timeout
                  while the control proving otherwise passed locally.
  * service.py  - three operations were added and never deployed, so the running HTTP
                  API advertised eight operations instead of eleven.
  * mcp_api.py  - three MCP tools, same story.
  * panel_forecast.py - same story.

Every one of those passed its test. A test proves the REPOSITORY is right; it says
nothing about what is running. The gap is not subtle and it is not rare - it is the
default outcome of changing code without a deployment step that compares the two.

USAGE
-----
On the machine that holds the repository, write the manifest of what SHOULD be there:

    python tools/deploy_check.py --make deploy_manifest.json --root .

Then on the deployed host:

    python tools/deploy_check.py --check deploy_manifest.json --root /home/zixen15/attest-mcp2

Exit 0 means every listed file matches. Exit 1 means at least one drifted or is
missing. Exit 2 means the check could not be performed - which is not a pass.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
import time

# What the deployment is expected to contain. Kept as an explicit list rather than a
# glob, so that a file which disappears shows up as missing instead of quietly
# dropping out of the comparison.
DEFAULT_PATHS = (
    "attest/__init__.py", "attest/__main__.py", "attest/checkers.py",
    "attest/decisions.py", "attest/forecast.py", "attest/http_api.py",
    "attest/mcp_api.py", "attest/service.py", "attest/store.py",
    "attest/trust.py", "attest/verdict.py",
    "examples/panel_forecast.py",
    "tests/test_conformance.py", "tests/run_all.py",
    "tools/deploy_check.py", "tools/deploy_to_throne.sh",
    "tools/fleet_coverage.py",
    "tools/fleet_sweep.py", "tools/fleet_trust.py",
)


def digest(path: str) -> str:
    h = hashlib.md5()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def make(root: str, paths) -> dict:
    out = {}
    for rel in paths:
        full = os.path.join(root, rel)
        if os.path.isfile(full):
            out[rel] = digest(full)
    return out


def process_check(root: str, manifest: dict, patterns):
    """Long-lived processes must be running the code that is on disk.

    WHY THIS EXISTS, found on 2026-10-02. The HTTP API process had been started at
    15:36 and every file it loads had been replaced since. It advertised EIGHT
    operations while the file on disk had ELEVEN, and - far worse - it silently
    dropped due_at, stratum, cohort and target from every request. A caller
    recording a FORECAST got a retrospective back, with "ok": true and no warning.

    The file check could not see any of this. Every file was in sync; the PROCESS
    was old. A deployment check that only looks at files will always miss the
    process, and a process is what actually serves the traffic.
    """
    newest, newest_rel = 0.0, ""
    for rel in manifest:
        full = os.path.join(root, rel)
        if os.path.isfile(full):
            m = os.path.getmtime(full)
            if m > newest:
                newest, newest_rel = m, rel
    if not newest:
        return [], []
    stale, fresh = [], []
    now = time.time()
    # EXCLUDE OURSELVES AND THE WHOLE CHAIN THAT SPAWNED US.
    #
    # pgrep -f matches a full command line, and this script is invoked as
    # "deploy_check.py --check ... --running 'attest serve'" - so the pattern is in
    # its own cmdline and it matches itself. Observed 2026-10-02: "process ok
    # attest serve (pid 4177608)" for a process that was this script.
    #
    # Excluding getpid() and getppid() was NOT enough. The runner wraps this in
    # "timeout 180 /bin/bash -c '... --running "attest serve"'", and the timeout
    # ancestor carries the pattern too, so two phantoms survived the first fix -
    # always freshly started, always reading as healthy, inflating the count in a
    # list somebody is scanning. Walk the whole ancestor chain instead; the first
    # fix was the same bug, incompletely repaired.
    mine = set()
    pid = os.getpid()
    for _ in range(16):
        if not pid or pid in mine:
            break
        mine.add(pid)
        try:
            with open("/proc/%d/status" % pid) as fh:
                for line in fh:
                    if line.startswith("PPid:"):
                        pid = int(line.split()[1])
                        break
                else:
                    break
        except Exception:
            break
    for pattern in patterns:
        try:
            out = subprocess.run(["pgrep", "-f", pattern], capture_output=True,
                                 text=True, timeout=20).stdout.split()
        except Exception:
            continue
        for pid in out:
            try:
                if int(pid) in mine:
                    continue
            except ValueError:
                continue
            try:
                et = subprocess.run(["ps", "-o", "etimes=", "-p", pid],
                                    capture_output=True, text=True,
                                    timeout=20).stdout.strip()
                started = now - float(et)
            except Exception:
                continue
            entry = {"pattern": pattern, "pid": int(pid),
                     "started_age_s": round(now - started),
                     "newest_file": newest_rel,
                     "newest_age_s": round(now - newest)}
            (stale if started < newest else fresh).append(entry)
    return stale, fresh


def check(root: str, manifest: dict):
    in_sync, drifted, missing = [], [], []
    for rel, want in sorted(manifest.items()):
        full = os.path.join(root, rel)
        if not os.path.isfile(full):
            missing.append(rel)
            continue
        got = digest(full)
        (in_sync if got == want else drifted).append(rel)
    return in_sync, drifted, missing


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", default=".")
    ap.add_argument("--make")
    ap.add_argument("--check")
    ap.add_argument("--running", action="append", default=[], metavar="PATTERN",
                    help="a long-lived process whose code must match the files, "
                         "e.g. --running 'attest serve'. Repeatable.")
    ap.add_argument("--quiet", action="store_true")
    args = ap.parse_args()

    if args.make:
        m = make(args.root, DEFAULT_PATHS)
        if not m:
            print("nothing found under %s - refusing to write an empty manifest"
                  % args.root)
            return 2
        with open(args.make, "w") as fh:
            json.dump(m, fh, indent=1, sort_keys=True)
        print("wrote %d file hashes to %s" % (len(m), args.make))
        return 0

    if not args.check:
        ap.error("one of --make or --check is required")
    try:
        with open(args.check) as fh:
            manifest = json.load(fh)
    except Exception as exc:
        print("could not read the manifest %s: %s" % (args.check, exc))
        return 2

    in_sync, drifted, missing = check(args.root, manifest)
    stale_procs, fresh_procs = ([], [])
    if args.running:
        stale_procs, fresh_procs = process_check(args.root, manifest, args.running)

    if not args.quiet:
        for rel in in_sync:
            print("  in sync  %s" % rel)
        for rel in drifted:
            print("  DRIFTED  %s" % rel)
        for rel in missing:
            print("  MISSING  %s" % rel)

    for p in stale_procs:
        print("  STALE PROCESS  %s (pid %d) started %ds ago; %s changed %ds ago"
              % (p["pattern"], p["pid"], p["started_age_s"], p["newest_file"],
                 p["newest_age_s"]))
    for p in fresh_procs:
        print("  process ok     %s (pid %d)" % (p["pattern"], p["pid"]))

    print("=" * 62)
    print("%d in sync, %d drifted, %d missing, of %d expected; %d stale process(es)"
          % (len(in_sync), len(drifted), len(missing), len(manifest),
             len(stale_procs)))
    if drifted or missing:
        print("THE DEPLOYMENT IS NOT THE CODE THAT WAS TESTED. A passing test suite "
              "says nothing about what is running.")
        return 1
    if stale_procs:
        print("THE FILES ARE IN SYNC AND A LONG-LIVED PROCESS IS NOT. It loaded its "
              "code before these files changed, so what it serves is older than what "
              "was tested - and it will keep serving it until it is restarted.")
        return 1
    print("the deployment matches the manifest"
          + (", and every checked process is running it" if args.running else ""))
    return 0


if __name__ == "__main__":
    sys.exit(main())
