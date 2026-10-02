#!/bin/bash
# fleet_restart_api.sh - restart the long-lived ledger API and prove it came back.
#
# WHY THIS IS A FILE AND NOT AN INLINE ssh COMMAND. It was inline first, with nested
# quoting inside a generated shell script, and it broke: the pkill succeeded and the
# restart did not, leaving the estate with no API at all. A deploy step that can take
# the service down and fail to bring it up is worse than one that leaves it stale.
#
# So: one file, no quoting to get wrong, and it FAILS LOUDLY if the port does not come
# back. A restart that silently does nothing is the failure this whole session is about.
set -u
DEST=/home/zixen15/attest-mcp2
PORT=8260
LOG=/tmp/attest_api.log

was_running=no
pgrep -f "attest serve --port $PORT" >/dev/null && was_running=yes

if [ "$was_running" = yes ]; then
  pkill -f "attest serve --port $PORT" || true
  sleep 2
fi

cd "$DEST" || exit 2
setsid nohup env ATTEST_HOME=/home/zixen15/.attest-ledger \
  /usr/bin/python3 -m attest serve --port $PORT > "$LOG" 2>&1 &
sleep 6

OPS=$(curl -s --max-time 8 "http://127.0.0.1:$PORT/attest/v1/manifest" 2>/dev/null \
      | /usr/bin/python3 -c 'import sys,json;print(len(json.load(sys.stdin)["operations"]))' 2>/dev/null)

if [ -z "$OPS" ]; then
  echo "THE API DID NOT COME BACK on port $PORT after a restart (was_running=$was_running)." >&2
  echo "--- last lines of $LOG ---" >&2
  tail -n 10 "$LOG" >&2 || true
  exit 1
fi

echo "   restarted (was_running=$was_running); manifest advertises $OPS operations"
exit 0
