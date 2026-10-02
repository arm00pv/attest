#!/usr/bin/env python3
"""fleet_sweep.py - the consequential stratum: what will the estate find tomorrow?

WHY THIS ONE MATTERS MORE THAN THE PANEL
----------------------------------------
The panel stratum asks whether load average stays under a threshold, which is a
question about a number. This asks: at the next job-liveness sweep, which of the
scheduled things this estate watches will be reported as broken? That is the
question the estate exists to answer, and getting it wrong is why the ESP32
boards were chased for three days.

The truth arrives by itself from an instrument that already runs and neither
knows nor cares what was predicted. The horizon is overridable so this path can be
exercised now rather than discovered to be broken tomorrow: a resolver that is
only tested once a day takes a day to find out about.
"""
import argparse
import json
import os
import subprocess
import sys

sys.path.insert(0, "/home/zixen15/attest-mcp2")
from attest import Store, DecisionLedger                       # noqa: E402
from attest.decisions import now_iso                           # noqa: E402
from attest.forecast import (BaseRate, Constant, Item, Persistence, record_forecasts,
                             resolve_due)  # noqa: E402

HOME = "/home/zixen15"
STATE = HOME + "/.omni_brain/job_liveness_state.json"
HIST = HOME + "/.omni_brain/fleet_sweep_history.json"
STRATUM = "sweep1d"


def load_history():
    try:
        with open(HIST) as fh:
            return {k: [bool(x) for x in v] for k, v in json.load(fh).items()}
    except Exception:
        return {}


def save_history(h):
    tmp = HIST + ".tmp"
    with open(tmp, "w") as fh:
        json.dump(h, fh)
    os.replace(tmp, HIST)


def sweep_json():
    """Re-measure with an instrument that does not know what was predicted."""
    r = subprocess.run(["timeout", "240", "python3", HOME + "/sentinel/job_liveness.py",
                        "--json"], capture_output=True, text=True)
    # rc 1 means it found something, which is a result, not a failure.
    if r.returncode not in (0, 1):
        return None
    try:
        return json.loads(r.stdout)
    except Exception:
        return None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--hours", type=float, default=24.0)
    ap.add_argument("--db", default=os.environ.get("ATTEST_DB")
                    or (HOME + "/.attest-ledger/attest.db"))
    ap.add_argument("--history", default=HIST)
    ap.add_argument("--state", default=STATE)
    ap.add_argument("--stratum", default=STRATUM)
    ap.add_argument("--record", action="store_true",
                    help="record the next batch as well as settling")
    args = ap.parse_args()

    sw = sweep_json()
    if sw is None:
        print("the sweep did not report; every open forecast stays OPEN rather "
              "than being guessed at")
        return 2
    findings = set(f.get("source", "") for f in sw.get("findings", []))
    try:
        with open(args.state) as fh:
            universe = sorted(json.load(fh).keys())
    except Exception:
        universe = []
    if not universe:
        print("no target universe: %s is missing or empty" % args.state)
        return 2

    ledger = DecisionLedger(Store(args.db))
    history = load_history()
    observed = {t: (t in findings) for t in universe}

    def settle(dec):
        if dec.get("stratum") != args.stratum:
            return None            # the panel driver settles its own
        t = dec["target"]
        if t not in observed:
            return None            # the target is gone; not knowable, stays open
        return ("in findings" if observed[t] else "not in findings", observed[t])

    resolved = resolve_due(ledger, settle, now=now_iso())

    print("sweep %s: %d target(s), %d in findings"
          % (now_iso(), len(universe), len(findings)))
    print("settled %d, %d still open, %d refused"
          % (resolved["settled"], len(resolved["still_open"]), len(resolved["refused"])))
    for d in resolved["resolutions"][:6]:
        called = (1 if (d["p"] or 0.0) >= 0.5 else 0)
        print("   %-34s p=%.3f  outcome=%-5s  called=%-5s  %s"
              % (d["target"][:34], d["p"] or 0.0,
                 "true" if d["correct"] else "false",
                 "true" if called else "false",
                 "RIGHT" if called == int(bool(d["correct"])) else "wrong"))

    if args.record:
        items = [Item(t, "will %s be in findings when job-liveness is re-run about a day from now?" % t,
                      context="sweep at " + now_iso() + "; currently "
                              + ("IN FINDINGS" if observed[t] else "clean"),
                      stratum=args.stratum) for t in universe]
        written = record_forecasts(
            ledger, items,
            # Constant(0.5) scores a Brier of exactly 0.25 on any population, so it is
            # the fixed line this stratum is measured against. Worse than 0.25 is worse
            # than knowing nothing at all.
            [Constant(0.5, "null-0.50"), BaseRate(history),
             Persistence(observed, noise=0.005)],
            cohort=args.stratum + "-" + now_iso(),
            due_at=now_iso(max(1, int(args.hours * 3600))),
            who="fleet-sweep", history=history)
        print("recorded %d at +%.3gh" % (written["recorded"], args.hours))
        for t in universe:
            history.setdefault(t, []).append(bool(observed[t]))
            history[t] = history[t][-400:]
        save_history(history)
    return 0


if __name__ == "__main__":
    sys.exit(main())

