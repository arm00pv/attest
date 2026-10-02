#!/bin/bash
# api_guard.sh - the body of the ledger API check, with no runner machinery in it.
#
# Split out from fleet_apiguard_run.sh so that the wrapper can delegate to fleet_run.sh
# like every other job. The first version put this logic IN the wrapper, which meant it
# bypassed the shared runner entirely - no heartbeat, no anomaly log, no notification.
# Measured: job_beat rc=1 with no heartbeat written and no log, which is a job that fails
# SILENTLY, in the one session whose whole subject is jobs that fail silently.
set -u
PORT=8260

if curl -s --max-time 8 "http://127.0.0.1:$PORT/attest/v1/health" >/dev/null 2>&1; then
  echo "the ledger API answered on $PORT"
  exit 0
fi

echo "the ledger API did NOT answer on $PORT - attempting a restart"
/home/zixen15/bin/fleet_restart_api.sh
rc=$?
if [ "$rc" -ne 0 ]; then
  echo "AND THE RESTART FAILED (rc=$rc). The product surface is DOWN."
  exit 2
fi

# Restored, and still non-zero: an API that had to be restarted is an incident, not a
# normal cycle, and it should reach the beat file and the notification path.
echo "RESTORED - the API had stopped and has been restarted. Something killed it and"
echo "nothing here knows what; the process log is /tmp/attest_api.log"
exit 1
