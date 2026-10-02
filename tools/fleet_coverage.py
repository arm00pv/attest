#!/usr/bin/env python3
"""fleet_coverage.py - did every scheduled run actually produce a result?

WHY THIS IS NOT THE SAME AS job_liveness
----------------------------------------
job_liveness answers "did the job run?", from beat files and the journal. This
answers a different question that nothing else asks: did the run produce anything?
A job can start, exit 0, write its heartbeat, stamp its beat file, and record no
decisions at all - because a lock was held, because a data source was empty,
because a guard returned early. Every liveness signal says it is healthy.

The ledger is the only durable record of runs that actually produced results. Each
run writes one cohort, so the spacing between cohorts is the run history, and a
missing cycle is a visible gap rather than an absence nobody can see.

Found on its first real run, 2026-10-02: a 29.9-minute gap in the 15-minute panel
where the 15:00 run had died with "database is locked". The beat file had been
refreshed by a later run and job_liveness saw nothing wrong.
"""
import argparse
import os
import re
import sys

sys.path.insert(0, "/home/zixen15/attest-mcp2")
from attest import Store                                                    # noqa: E402
from attest.decisions import _iso_to_epoch                                  # noqa: E402

DB = os.environ.get("ATTEST_DB") or "/home/zixen15/.attest-ledger/attest.db"
ISO = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")

# Each stratum and the period it is supposed to run at, in seconds. A stratum not
# listed here has no claimed cadence and cannot be said to have missed one.
PERIODS = {"panel15m": 900, "panel1h": 3600, "sweep1d": 86400, "hosttrust": 21600}

# How much slack before a gap counts. Timers drift and a run can be a little late;
# 1.6 periods means a gap of two whole cycles is always reported and a few seconds
# of jitter never is.
SLACK = 1.6


def stamp(cohort):
    tail = (cohort or "")[-20:]
    return tail if ISO.match(tail) else None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=DB)
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    s = Store(args.db)
    report, problems, measured = [], [], 0

    for stratum, period in sorted(PERIODS.items()):
        rows = s.db.execute("select distinct cohort from decisions where stratum=?",
                            (stratum,)).fetchall()
        ts = sorted(t for t in (stamp(r["cohort"]) for r in rows) if t)
        if len(ts) < 2:
            report.append({"stratum": stratum, "cohorts": len(ts),
                           "verdict": "NOT ENOUGH RUNS TO JUDGE YET",
                           "gaps": []})
            continue
        measured += 1
        gaps = []
        for a, b in zip(ts, ts[1:]):
            d = _iso_to_epoch(b) - _iso_to_epoch(a)
            if d > period * SLACK:
                gaps.append({"from": a, "to": b,
                             "minutes": round(d / 60.0, 1),
                             "period_minutes": period // 60,
                             "missed": int(round(d / period)) - 1})
        report.append({"stratum": stratum, "cohorts": len(ts), "first": ts[0],
                       "last": ts[-1], "period_minutes": period // 60,
                       "gaps": gaps,
                       "verdict": ("GAPS" if gaps else "complete")})
        for g in gaps:
            problems.append("%s missed %d run(s): %s -> %s (%.1f min against a "
                            "%d-minute period)"
                            % (stratum, g["missed"], g["from"], g["to"],
                               g["minutes"], g["period_minutes"]))

    if args.json:
        import json
        print(json.dumps({"ok": True, "strata": report, "problems": problems},
                         indent=2, sort_keys=True))
    else:
        for r in report:
            line = "%-11s %3d cohort(s)" % (r["stratum"], r["cohorts"])
            if r.get("period_minutes"):
                line += "  period %2dm" % r["period_minutes"]
            line += "  -> %s" % r["verdict"]
            print(line)
            for g in r.get("gaps", []):
                print("      MISSED %d: %s -> %s (%.1f min)"
                      % (g["missed"], g["from"], g["to"], g["minutes"]))
        print()
        if problems:
            print("SCHEDULED RUNS THAT PRODUCED NOTHING:")
            for p in problems:
                print("  " + p)
        else:
            print("every stratum with a claimed cadence has an unbroken record")

    if problems:
        return 1
    if measured == 0:
        # Nothing had enough history to judge. That is not a pass.
        print("NOT MEASURED: no stratum has two runs yet, so no cadence could be "
              "checked")
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
