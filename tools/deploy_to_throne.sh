#!/usr/bin/env bash
# deploy_to_throne.sh - regenerate the manifest FROM THE REPO and push it, in one step.
#
# WHY THIS EXISTS. The manifest was regenerated from the wrong directory twice, and
# both times the deployment check caught it afterwards - 16 in sync, 2 drifted. The
# second time it happened I had already committed a message about the first. When the
# same slip happens twice the fault is the process, not the attention: regenerating
# the manifest and copying it were two separate steps that had to agree about which
# directory they were in, and nothing enforced that.
#
# This script only works from the repository root, and refuses to run anywhere else.
set -euo pipefail

HOST="${THRONE_HOST:-zixen15@100.113.24.19}"
KEY="${THRONE_KEY:-$HOME/.ssh/id_ed25519_throne}"
DEST=/home/zixen15/attest-mcp2

ROOT=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
if [ ! -f "$ROOT/attest/store.py" ]; then
  echo "deploy_to_throne.sh: $ROOT does not look like the attest repository" >&2
  exit 2
fi
cd "$ROOT"

echo "== regenerating the manifest from $ROOT =="
python3 tools/deploy_check.py --make deploy_manifest.json --root .

echo "== copying the tracked tree =="
scp -q -o StrictHostKeyChecking=no -i "$KEY" \
  attest/*.py "$HOST:$DEST/attest/"
scp -q -o StrictHostKeyChecking=no -i "$KEY" \
  examples/panel_forecast.py "$HOST:$DEST/examples/"
# .sh as well as .py: the deploy script itself is tracked, and the first run of
# this file reported it as MISSING because only *.py was being copied.
scp -q -o StrictHostKeyChecking=no -i "$KEY" \
  tools/*.py tools/*.sh "$HOST:$DEST/tools/"
scp -q -o StrictHostKeyChecking=no -i "$KEY" \
  tests/test_conformance.py tests/run_all.py "$HOST:$DEST/tests/"
scp -q -o StrictHostKeyChecking=no -i "$KEY" deploy_manifest.json "$HOST:$DEST/"

# RESTART THE LONG-LIVED PROCESS, or the deployment is only half done.
#
# Files landing on disk does not change what a running server serves. On 2026-10-02 the
# API process served EIGHT operations while disk had ELEVEN and silently dropped due_at
# from every request - turning forecasts into retrospectives with "ok": true - for over
# an hour, while every file hash matched. Deploying and restarting are one operation.
# RESTART THE LONG-LIVED PROCESS, or the deployment is only half done.
#
# Files landing on disk does not change what a running server serves. On 2026-10-02 the
# API process served EIGHT operations while disk had ELEVEN and silently dropped due_at
# from every request - turning forecasts into retrospectives with "ok": true - for over
# an hour, while every file hash matched. Deploying and restarting are one operation.
#
# THE RESTART IS A FILE ON THE HOST, not an inline ssh command. It was inline first, with
# nested quoting inside a generated script, and it broke: the pkill worked and the restart
# did not, leaving the estate with no API at all. A deploy step that can take the service
# down and fail to bring it up is worse than one that leaves it stale.
echo "== restarting the long-lived API so it serves the new code =="
ssh -o StrictHostKeyChecking=no -i "$KEY" "$HOST" \
  "bash $DEST/bin_restart_api.sh 2>/dev/null || bash /home/zixen15/bin/fleet_restart_api.sh"
restart_rc=$?
if [ "$restart_rc" -ne 0 ]; then
  echo "THE API DID NOT COME BACK. The files are deployed and the service is DOWN." >&2
  exit "$restart_rc"
fi

echo "== verifying what landed, and what is running it =="
ssh -o StrictHostKeyChecking=no -i "$KEY" "$HOST" \
  "cd $DEST && python3 tools/deploy_check.py --check deploy_manifest.json --root . --quiet --running 'attest serve'"
rc=$?
if [ "$rc" -ne 0 ]; then
  echo "DEPLOY FAILED VERIFICATION (exit $rc) - the deployment is not the code that" >&2
  echo "was tested. Do not trust it until this passes." >&2
fi
exit "$rc"
