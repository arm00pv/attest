#!/bin/bash
# Is the ledger API still up, and say so either way.
#
# WHY THIS EXISTS. The API on port 8260 is the product surface for the ledger, and it was
# started with setsid nohup - so NOTHING supervises it. The older attest service on 8250 is
# managed by systemd and would be restarted; this one stays down until somebody notices. On
# 2026-08-02 it was down for several minutes after a broken deploy and nothing said a word.
#
# A watchdog that silently restarts is worse than one that does not, because it hides the
# outage. This one RESTARTS and SHOUTS - and it does that through fleet_run.sh, like every
# other job, so the shout is real.
FLEET_REQUIRE_DB=/home/zixen15/.attest-ledger/attest.db \
  exec /home/zixen15/attest-mcp2/bin/fleet_run.sh apiguard 180 \
    /bin/bash /home/zixen15/attest-mcp2/bin/api_guard.sh
