#!/bin/bash
# install_service.sh - put the ledger API under systemd, once, on a new host.
#
# Run with sudo. Idempotent: safe to run twice. It VERIFIES the port comes back and
# ROLLS BACK if it does not - a service install that leaves the product surface down
# is worse than not installing it.
#
#   sudo /home/zixen15/attest-mcp2/tools/install_service.sh
#
# Everything it writes comes from a file next to it in deploy/. Nothing is generated
# by a heredoc, because nested quoting inside a generated shell script is the exact
# defect that once took the API down and failed to bring it back.
set -u

HERE=$(cd "$(dirname "$0")" && pwd)
DEPLOY=$(cd "$HERE/../deploy" && pwd)
UNIT=attest-mcp2.service
GUARD=fleet_restart_api.sh
PORT=8260
RUNAS=zixen15
RUNHOME=/home/zixen15
SUDOERS=/etc/sudoers.d/attest-mcp2
UNITPATH=/etc/systemd/system/$UNIT
GUARDPATH=$RUNHOME/bin/$GUARD

if [ "$(id -u)" != "0" ]; then
  echo "must run as root:  sudo $0" >&2
  exit 2
fi
for f in "$DEPLOY/$UNIT" "$DEPLOY/$GUARD"; do
  if [ ! -f "$f" ]; then
    echo "missing deployment file: $f" >&2
    exit 2
  fi
done

rollback() {
  echo "rolling back: removing the unit and the sudoers rule" >&2
  systemctl disable --now "$UNIT" >/dev/null 2>&1 || true
  rm -f "$UNITPATH" "$SUDOERS"
  systemctl daemon-reload >/dev/null 2>&1 || true
  echo "rolled back. The previous launch path is untouched." >&2
}

echo "1/6  installing $UNITPATH"
install -m 0644 -o root -g root "$DEPLOY/$UNIT" "$UNITPATH" || exit 2

echo "2/6  sudoers rule so $RUNAS can restart only this unit"
cat > "$SUDOERS" <<SUDOEOF
# Installed by $HERE/install_service.sh
# fleet_apiguard runs from cron as $RUNAS and must be able to bring the ledger API
# back when it finds it down. Two verbs, one unit, no wildcards.
$RUNAS ALL=(root) NOPASSWD: /usr/bin/systemctl restart $UNIT
$RUNAS ALL=(root) NOPASSWD: /usr/bin/systemctl start $UNIT
$RUNAS ALL=(root) NOPASSWD: /bin/systemctl restart $UNIT
$RUNAS ALL=(root) NOPASSWD: /bin/systemctl start $UNIT
SUDOEOF
chmod 0440 "$SUDOERS"
if ! visudo -cf "$SUDOERS" >/dev/null 2>&1; then
  echo "the sudoers rule does not validate - removing it rather than breaking sudo" >&2
  rm -f "$SUDOERS"
  exit 2
fi

echo "3/6  enabling and starting"
systemctl daemon-reload || { rollback; exit 2; }
systemctl enable "$UNIT" >/dev/null 2>&1 || { rollback; exit 2; }

# A bare instance from the old launch path would fight this one for the port.
if pgrep -f "attest serve --port $PORT" >/dev/null 2>&1; then
  echo "     stopping the unmanaged instance holding port $PORT"
  pkill -f "attest serve --port $PORT" || true
  sleep 2
fi
systemctl restart "$UNIT" || { rollback; exit 2; }

echo "4/6  waiting for the port to answer"
OPS=""
for _ in 1 2 3 4 5 6 7 8 9 10; do
  sleep 2
  OPS=$(curl -s --max-time 5 "http://127.0.0.1:$PORT/attest/v1/manifest" 2>/dev/null \
        | /usr/bin/python3 -c 'import sys,json;print(len(json.load(sys.stdin)["operations"]))' 2>/dev/null)
  [ -n "$OPS" ] && break
done
if [ -z "$OPS" ]; then
  echo "THE API DID NOT COME BACK on port $PORT." >&2
  journalctl -u "$UNIT" -n 20 --no-pager >&2 || true
  rollback
  exit 1
fi
echo "     API up; manifest advertises $OPS operations"

echo "5/6  installing the systemd-aware guard at $GUARDPATH"
if [ -f "$GUARDPATH" ] && ! grep -q 'systemctl restart' "$GUARDPATH"; then
  cp -a "$GUARDPATH" "$GUARDPATH.bak_pre_systemd"
  echo "     previous version saved at $GUARDPATH.bak_pre_systemd"
fi
install -m 0755 -o "$RUNAS" -g "$RUNAS" "$DEPLOY/$GUARD" "$GUARDPATH" || { rollback; exit 2; }

echo "6/6  exercising the guard as $RUNAS"
if sudo -u "$RUNAS" "$GUARDPATH"; then
  echo "     the guard restarted the API through systemd and it answered"
else
  echo "THE GUARD PATH FAILED: the unit is installed but fleet_apiguard cannot use it." >&2
  exit 1
fi

echo ""
echo "done. $UNIT is enabled and active; the API is restarted by the guard through"
echo "systemd, and it will start by itself after a reboot."
exit 0
