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
run writes one cohort, so the spacing between cohorts is the run history.

Found on its first real run, 2026-10-02: a 29.9-minute gap in the 15-minute panel
where the 15:00 run had died with "database is locked". The beat file had been
refreshed by a later run and job_liveness saw nothing wrong.

AND THE HOLE IN THE FIRST VERSION OF THIS FILE
----------------------------------------------
It compared consecutive cohorts and so could only see a gap BETWEEN two runs. A
stratum that stopped entirely writes no further cohorts, so there is no pair to
compare and it reported "complete" forever - the instrument blind to its own
silence, which is the exact defect this whole file exists to catch. Demonstrated
before fixing it: three hourly cohorts ending at 12:00, read at 20:00, came back
"complete" while the job had been dead for eight hours.

The last cohort is now compared against NOW as well as against its predecessor.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from attest import Store                                                    # noqa: E402
from attest.decisions import _iso_to_epoch, now_iso                         # noqa: E402

DB = os.environ.get("ATTEST_DB") or "/home/zixen15/.attest-ledger/attest.db"
ISO = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")

# Each stratum and the period it is supposed to run at, in seconds. A stratum not
# listed here has no claimed cadence and cannot be said to have missed one.
PERIODS = {"panel15m": 900, "panel1h": 3600, "sweep1d": 86400, "hosttrust": 21600}

# How much slack before a gap or a silence counts. Timers drift and a run can be a
# little late; 1.6 periods means two whole missed cycles are always reported and a
# few seconds of jitter never is.
SLACK = 1.6


def stamp(cohort):
    tail = (cohort or "")[-20:]
    return tail if ISO.match(tail) else None


def analyse(series, periods=None, now=None, slack=SLACK, known=None,
            targets=None):
    """Pure. series is {stratum: [iso timestamps]}; returns the report and problems.

    Kept free of the database and with an injectable clock so it can be controlled:
    a check that can only be exercised by waiting for a real job to die is not a
    check anybody will ever run.

    A GAP ALREADY REPORTED IS NOT REPORTED AGAIN. The first version failed on any
    gap it found, and a gap is permanent: the 15:00 panel gap of 2026-10-02 will
    still be in the ledger next year. So the check exited 1 forever, job_liveness
    reported "its last run exited 1" forever, and the notification fired every six
    hours about something already fixed. That is the cry-wolf this estate has
    already written an addendum about - a detector that trains its reader to ignore
    it, after which the real one is ignored too.

    A STALL is different and always fails: it is a condition that is true right
    now, not a mark left in the past.
    """
    periods = PERIODS if periods is None else periods
    now = now or now_iso()
    now_e = _iso_to_epoch(now)
    known = set(known or ())
    targets = targets or {}
    report, problems = [], []

    for stratum in sorted(periods):
        period = periods[stratum]
        ts = sorted(t for t in (stamp(c) for c in (series.get(stratum) or [])) if t)
        entry = {"stratum": stratum, "cohorts": len(ts),
                 "period_minutes": period // 60, "gaps": []}
        age = None
        if ts:
            entry["first"], entry["last"] = ts[0], ts[-1]
            age = now_e - _iso_to_epoch(ts[-1])
            entry["age_minutes"] = round(age / 60.0, 1)

        # THE SILENCE CHECK. A stratum whose most recent cohort is older than its
        # own period has stopped, and no comparison between cohorts can reveal it.
        if age is not None and age > period * slack:
            entry["verdict"] = "STALLED"
            problems.append("%s has produced nothing for %.1f min against a "
                            "%d-minute period - the job has stopped, and this "
                            "cannot be seen by comparing runs to each other"
                            % (stratum, age / 60.0, period // 60))
            report.append(entry)
            continue

        for a, b in zip(ts, ts[1:]):
            d = _iso_to_epoch(b) - _iso_to_epoch(a)
            if d > period * slack:
                entry["gaps"].append({"from": a, "to": b,
                                      "minutes": round(d / 60.0, 1),
                                      "missed": int(round(d / period)) - 1})
        for g in entry["gaps"]:
            key = "%s|%s|%s" % (stratum, g["from"], g["to"])
            g["already_reported"] = key in known
            if not g["already_reported"]:
                problems.append("%s missed %d run(s): %s -> %s (%.1f min against a "
                                "%d-minute period)"
                                % (stratum, g["missed"], g["from"], g["to"],
                                   g["minutes"], period // 60))

        # DID THE RUN COVER EVERYTHING IT WAS SUPPOSED TO?
        #
        # A run can succeed, exit 0, stamp its beat and record a cohort while
        # silently dropping half its items. Found on this estate: two panel runs
        # recorded 13 targets where every other run recorded 14, because one item
        # could not be measured, and NOTHING SAID SO - the heartbeat was green, the
        # beat was stamped, and this file saw a cohort and called it complete.
        #
        # A DROP is the signal, not a difference: the sweep's target universe grows
        # as new timers appear, so 40 then 43 is healthy. 14 then 13 is not.
        #
        # TWO DIMENSIONS, NOT ONE. Counting targets alone misses a FORECASTER
        # dropping out: if the model stops answering, the panel still covers all
        # fourteen targets with the baselines, so the target count is unchanged
        # while decisions fall from 56 to 42. Demonstrated before this was written:
        # targets constant at 14 with decisions 56 -> 42 reported "complete".
        tser = []
        for item in (targets.get(stratum) or []):
            if isinstance(item, (list, tuple)) and len(item) >= 2:
                tser.append((item[0], item[1],
                             item[2] if len(item) > 2 else None))
        tser.sort()
        entry["partial"] = []
        for (a, na, ma), (b, nb, mb) in zip(tser, tser[1:]):
            if nb < na:
                entry["partial"].append({"from": a, "to": b, "what": "items",
                                         "was": na, "now": nb})
            if ma is not None and mb is not None and mb < ma:
                entry["partial"].append({"from": a, "to": b, "what": "forecasters",
                                         "was": ma, "now": mb})
        for p in entry["partial"]:
            key = "%s|partial|%s|%s|%s" % (stratum, p["what"], p["from"], p["to"])
            p["already_reported"] = key in known
            if not p["already_reported"]:
                problems.append(
                    "%s ran with %d %s where the run before it had %d - the run "
                    "succeeded and silently covered less (%s -> %s)"
                    % (stratum, p["now"], p["what"], p["was"], p["from"], p["to"]))

        if entry["gaps"]:
            entry["verdict"] = "GAPS"
        elif entry["partial"]:
            entry["verdict"] = "PARTIAL"
        elif len(ts) < 2:
            entry["verdict"] = "NOT ENOUGH RUNS TO JUDGE YET"
        else:
            entry["verdict"] = "complete"
        report.append(entry)

    return report, problems


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=DB)
    ap.add_argument("--now", default=None, help="override the clock (testing)")
    ap.add_argument("--state", default=os.path.join(
        os.path.expanduser("~"), ".omni_brain", "fleet_coverage_seen.json"),
        help="gaps already reported, so they are not re-alerted forever")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--quiet", action="store_true")
    args = ap.parse_args()
    # NOTE: argparse exits 2 on a usage error, which is the same code this file uses
    # for NOT MEASURED. A typo in a cron line would therefore read as "could not
    # measure" rather than "bad arguments". The usage text on stderr distinguishes
    # them in the log; the number does not. Recorded rather than papered over.

    try:
        with open(args.state) as fh:
            known = set(json.load(fh))
    except Exception:
        known = set()

    s = Store(args.db)
    series, targets = {}, {}
    for stratum in PERIODS:
        # GROUP BY cohort, not DISTINCT: without the count this measured runs and
        # not how much each run covered, and a stratum with 1634 decisions in 40
        # cohorts reported 1634 runs. The first rewrite of this file did exactly
        # that, and the item count is what catches a run that quietly covered less.
        rows = s.db.execute(
            "select cohort, count(distinct target) n, count(distinct model) m "
            "from decisions where stratum=? group by cohort", (stratum,)).fetchall()
        series[stratum] = [r["cohort"] for r in rows]
        targets[stratum] = [(stamp(r["cohort"]), r["n"], r["m"]) for r in rows
                            if stamp(r["cohort"])]

    report, problems = analyse(series, now=args.now, known=known, targets=targets)
    # PARTIAL must be counted here. It is a JUDGED verdict - the run was measured
    # and found to cover less - and leaving it out means a run of all-PARTIAL
    # strata would report "NOT MEASURED", which is the opposite of what happened.
    measured = sum(1 for r in report
                   if r["verdict"] in ("complete", "GAPS", "PARTIAL", "STALLED"))

    # Remember every gap seen, so the next run stays quiet about it.
    seen = set(known)
    for r in report:
        for g in r.get("gaps", []):
            seen.add("%s|%s|%s" % (r["stratum"], g["from"], g["to"]))
        for p in r.get("partial", []):
            seen.add("%s|partial|%s|%s|%s" % (r["stratum"], p["what"], p["from"],
                                              p["to"]))
    if seen != known:
        tmp = args.state + ".tmp"
        try:
            with open(tmp, "w") as fh:
                json.dump(sorted(seen), fh, indent=1)
            os.replace(tmp, args.state)
        except Exception as exc:
            print("could not record the gaps seen: %s" % exc)

    if args.json:
        print(json.dumps({"ok": True, "strata": report, "problems": problems},
                         indent=2, sort_keys=True))
    else:
        for r in report:
            line = "%-11s %3d cohort(s)" % (r["stratum"], r["cohorts"])
            if r.get("period_minutes"):
                line += "  period %3dm" % r["period_minutes"]
            if r.get("age_minutes") is not None:
                line += "  last %6.1fm ago" % r["age_minutes"]
            line += "  -> %s" % r["verdict"]
            print(line)
            for g in r.get("gaps", []):
                print("      MISSED %d: %s -> %s (%.1f min)%s"
                      % (g["missed"], g["from"], g["to"], g["minutes"],
                         "  [already reported]" if g.get("already_reported")
                         else ""))
            for p in r.get("partial", []):
                print("      PARTIAL: %s had %d %s, the run before it had %d%s"
                      % (p["to"], p["now"], p["what"], p["was"],
                         "  [already reported]" if p.get("already_reported")
                         else ""))
        print()
        if problems:
            print("SCHEDULED RUNS THAT PRODUCED NOTHING:")
            for p in problems:
                print("  " + p)
        else:
            print("every stratum with a claimed cadence has produced results on "
                  "schedule, and none has gone quiet")

    if problems:
        return 1
    if measured == 0:
        print("NOT MEASURED: no stratum could be judged, which is not a pass")
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
