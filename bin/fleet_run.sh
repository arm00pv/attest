#!/bin/bash
# fleet_run.sh - run one fleet job with the estate's full discipline.
#
#   fleet_run.sh <name> <timeout_seconds> <command...>
#
# WHY ONE SCRIPT AND NOT FOUR COPIES. On 2026-10-02 a scheduled panel run died with
# "sqlite3.OperationalError: database is locked", wrote one line to a log nobody
# reads, and the cycle was skipped. Nothing else noticed. Four runners copied from
# each other is how one of them ends up missing the notify step, so there is one.
#
# The exit code is the job's REAL exit code. job_liveness reads the rc field of the
# beat file and reports "its last run exited N", and every runner on this estate
# currently exits 0 unconditionally, which is why that field has always read zero
# and the check has never been able to fire.
set -u
NAME=${1:-}
if [ -z "$NAME" ]; then echo "fleet_run.sh: needs a job name" >&2; exit 2; fi
TMO=${2:-300}
shift 2
if [ "$#" -eq 0 ]; then echo "fleet_run.sh: no command for $NAME" >&2; exit 2; fi

BASE=/home/zixen15
LOCK=$BASE/logs/.fleet_$NAME.lock
LOG=$BASE/logs/fleet_${NAME}_anomalies.log
HB=$BASE/.omni_brain/fleet_$NAME.last_run
RCF=$BASE/.omni_brain/fleet_$NAME.last_rc

# systemctl --user needs a session bus and cron does not provide one. Measured
# 2026-10-02: without this the panel item about failed user units returned NOT
# MEASURABLE on every run, which looks exactly like the item being fine.
export XDG_RUNTIME_DIR=/run/user/$(id -u)
export DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/$(id -u)/bus

mkdir -p "$(dirname "$LOCK")" "$BASE/.omni_brain"

shout() {   # shout <rc> <message...>
  local rc=$1; shift
  local msg="$*"
  if [ -f "$LOG" ] && [ "$(stat -c%s "$LOG")" -gt 1048576 ]; then mv -f "$LOG" "$LOG.1"; fi
  { echo "=== $(date -Is) rc=$rc ==="; echo "$msg"; } >> "$LOG"
  printf '%s\n' "$msg" | \
    ESTATE_NOTIFY_REPO=$BASE/.omni_brain/notify \
    ESTATE_NOTIFY_STATE=$BASE/.omni_brain/fleet_notify.json \
    /usr/bin/python3 $BASE/bin/estate_notify.py \
      --host "$(hostname)" --title "throne: fleet job $NAME failed (rc=$rc)" \
      --tags rotating_light >>"$LOG" 2>&1 || true
}

exec 9>"$LOCK"
if ! flock -n 9; then
  # ANOTHER COPY HOLDS THE LOCK. That is normally the arrangement working and not a
  # job failure - but it MUST NOT BE SILENT.
  #
  # Measured 2026-10-02, with the lock deliberately held: the runner exited 0, left
  # the heartbeat untouched, wrote no log and sent no notification. A job that never
  # acquired its lock would therefore look perfectly healthy forever. Nothing on this
  # estate would have said a word.
  #
  # That is unreachable today only because every timeout happens to be shorter than
  # its own period, which is a coincidence rather than a guarantee. Raise one timeout
  # past its period and a permanently blocked job becomes invisible.
  #
  # 75 is EX_TEMPFAIL: the run did not happen, and it is not the job's fault.
  date -u +"%Y-%m-%dT%H:%M:%SZ" > "$HB"
  echo 75 > "$RCF"
  shout 75 "another copy of $NAME holds $LOCK; this run did nothing and the work it would have done was skipped"
  exit 75
fi

# A LEDGER THAT IS NOT THERE IS NOT A FRESH START.
#
# Store() creates a database when the path does not exist, which is right for a new
# install and catastrophic for a scheduled job. Measured 2026-10-02: pointed at a path
# with no ledger, all three writers - panel, trust and sweep - created a blank one and
# carried on, exit 0, every heartbeat green. Had the real ledger been lost the estate
# would have quietly started a new one and recorded into it, and nothing anywhere would
# have said the memory was gone.
#
# FLEET_REQUIRE_DB names the ledger this job is supposed to be writing to. If it is set
# and the file is not there, the job does not run.
if [ -n "${FLEET_REQUIRE_DB:-}" ] && [ ! -e "$FLEET_REQUIRE_DB" ]; then
  date -u +"%Y-%m-%dT%H:%M:%SZ" > "$HB"
  echo 70 > "$RCF"
  shout 70 "the ledger $FLEET_REQUIRE_DB does not exist, so $NAME did not run. The \
store would have created an empty one and recorded into it, which would look exactly \
like a healthy run while the history was gone."
  exit 70
fi

# 9>&- CLOSES THE LOCK DESCRIPTOR FOR THE CHILD AND EVERYTHING IT STARTS.
#
# Found on 2026-10-02: a job that launches a DAEMON handed it the lock. api_guard.sh
# called fleet_restart_api.sh, which started the ledger API with setsid nohup - and the
# new process inherited fd 9, the flock this runner holds. An inherited flock belongs to
# the process that still has the descriptor open, so the API held the apiguard lock
# forever:
#
#     zixen15 34694 F.... python3   /home/zixen15/logs/.fleet_apiguard.lock
#     the lock is HELD right now
#
# Every subsequent apiguard run then reported rc=75, "another copy holds the lock", which
# is a permanent false alarm from the signal added to catch exactly this class of thing.
# A short-lived child releases it when it exits; a daemon never does.
OUT=$(timeout "$TMO" "$@" 9>&- 2>&1)
rc=$?

# The heartbeat is stamped whether or not the job worked. A job that stopped
# running and a job that ran and found nothing must stay distinguishable.
date -u +"%Y-%m-%dT%H:%M:%SZ" > "$HB"
echo "$rc" > "$RCF"

if [ "$rc" -ne 0 ]; then
  shout "$rc" "$(printf '%s\n' "$OUT" | tail -n 20)"
  exit "$rc"
fi
exit 0
