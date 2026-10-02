#!/bin/bash
# Does the deployment on disk match the code that is actually running?
#
# WHY THIS IS SCHEDULED AND NOT JUST AVAILABLE. deploy_check existed and worked and
# nothing ran it, which is the same shape as the conformance suite that rotted because
# no CI ran it, and the ESP32 check that failed 860 times into a log nobody read. On
# 2026-10-02 the HTTP API served EIGHT operations while the file on disk had ELEVEN,
# and silently dropped due_at from every request, turning forecasts into retrospectives
# with "ok": true. Every file was in sync. Nothing was watching the process.
exec /home/zixen15/attest-mcp2/bin/fleet_run.sh deploycheck 180 \
  /bin/bash -c 'cd /home/zixen15/attest-mcp2 && exec /usr/bin/python3 tools/deploy_check.py --check deploy_manifest.json --root . --running "attest serve"'

