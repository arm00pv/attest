#!/bin/bash
# fleet_restart_api.sh - bring the ledger API back, and prove it came back.
#
# WHY THIS IS A FILE AND NOT AN INLINE ssh COMMAND. It was inline first, with nested
# quoting inside a generated shell script, and it broke: the pkill succeeded and the
# restart did not, leaving the estate with no API at all. A deploy step that can take
# the service down and fail to bring it up is worse than one that leaves it stale.
#
# REWRITTEN 2026-10-02 to go through systemd. The previous version pkilled the process
# and launched a bare replacement from this shell. Once the API became the unit
# attest-mcp2.service, that is no longer safe: the pkill would kill systemd's process,
# systemd would restart it, and the bare replacement would then race it for port 8260.
# Two processes, one port, one of them failing at random - which is the estate's
# doubled-name bug wearing a new coat.
set -u
PORT=8260
UNIT=attest-mcp2.service
LOG=/tmp/attest_api.log

if ! sudo -n /usr/bin/systemctl restart "$UNIT" 2>/dev/null; then
  echo "could not restart $UNIT through systemd." >&2
  echo "Either the unit is not installed on this host, or the sudoers rule in" >&2
  echo "/etc/sudoers.d/attest-mcp2 is missing. Run the installer:" >&2
  echo "    sudo /home/zixen15/attest-mcp2/tools/install_service.sh" >&2
  exit 1
fi

OPS=""
for _ in 1 2 3 4 5 6 7 8 9 10; do
  sleep 2
  OPS=$(curl -s --max-time 5 "http://127.0.0.1:$PORT/attest/v1/manifest" 2>/dev/null \
        | /usr/bin/python3 -c 'import sys,json;print(len(json.load(sys.stdin)["operations"]))' 2>/dev/null)
  [ -n "$OPS" ] && break
done

if [ -z "$OPS" ]; then
  echo "THE API DID NOT COME BACK on port $PORT after a systemd restart." >&2
  echo "--- journalctl -u $UNIT ---" >&2
  journalctl -u "$UNIT" -n 15 --no-pager >&2 || true
  echo "--- $LOG ---" >&2
  tail -n 10 "$LOG" >&2 || true
  exit 1
fi

echo "   restarted through systemd; manifest advertises $OPS operations"
exit 0
