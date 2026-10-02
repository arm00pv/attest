#!/bin/bash
# Fleet trust, just after each 6-hourly self-model check. --minutes 330 against a
# 6-hour timer.
FLEET_REQUIRE_DB=/home/zixen15/.attest-ledger/attest.db exec /home/zixen15/attest-mcp2/bin/fleet_run.sh trust 300 \
  /usr/bin/python3 /home/zixen15/attest-mcp2/tools/fleet_trust.py

