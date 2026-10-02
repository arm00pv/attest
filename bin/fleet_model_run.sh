#!/bin/bash
# The hourly model stratum. --minutes 55 against an hourly timer leaves the next
# run half an hour of clearance. keep_alive 2h so the model is not unloaded and
# reloaded, which would cost the full cold load every single hour.
FLEET_REQUIRE_DB=/home/zixen15/.attest-ledger/attest.db exec /home/zixen15/attest-mcp2/bin/fleet_run.sh model 900 \
  /usr/bin/python3 /home/zixen15/attest-mcp2/examples/panel_forecast.py \
    --config /home/zixen15/bin/fleet_panel.json \
    --db /home/zixen15/.attest-ledger/attest.db \
    --history /home/zixen15/.omni_brain/panel_history_model.json \
    --minutes 55 --stratum panel1h --who fleet-model \
    --model nimble --model-url http://127.0.0.1:11435 \
    --model-keep-alive 2h --model-timeout 840

