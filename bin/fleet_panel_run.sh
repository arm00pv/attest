#!/bin/bash
# The 15-minute panel. NOTE --minutes 14, not 15: a forecast due exactly one
# cadence ahead can miss its own settlement by seconds of jitter and wait a whole
# extra cycle.
FLEET_REQUIRE_DB=/home/zixen15/.attest-ledger/attest.db exec /home/zixen15/attest-mcp2/bin/fleet_run.sh panel 300 \
  /usr/bin/python3 /home/zixen15/attest-mcp2/examples/panel_forecast.py \
    --config /home/zixen15/bin/fleet_panel.json \
    --db /home/zixen15/.attest-ledger/attest.db \
    --history /home/zixen15/.omni_brain/panel_history.json \
    --minutes 14 --stratum panel15m --who fleet-panel

