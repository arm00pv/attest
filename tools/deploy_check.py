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
import sys

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
    "tools/deploy_check.py", "tools/fleet_coverage.py",
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
    if not args.quiet:
        for rel in in_sync:
            print("  in sync  %s" % rel)
        for rel in drifted:
            print("  DRIFTED  %s" % rel)
        for rel in missing:
            print("  MISSING  %s" % rel)

    print("=" * 62)
    print("%d in sync, %d drifted, %d missing, of %d expected"
          % (len(in_sync), len(drifted), len(missing), len(manifest)))
    if drifted or missing:
        print("THE DEPLOYMENT IS NOT THE CODE THAT WAS TESTED. A passing test suite "
              "says nothing about what is running.")
        return 1
    print("the deployment matches the manifest")
    return 0


if __name__ == "__main__":
    sys.exit(main())
