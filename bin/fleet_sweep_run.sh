#!/bin/bash
# The daily, consequential stratum. --hours 23.5 against a 24-hour timer.
FLEET_REQUIRE_DB=/home/zixen15/.attest-ledger/attest.db exec /home/zixen15/attest-mcp2/bin/fleet_run.sh sweep 420 \
  /usr/bin/python3 /home/zixen15/attest-mcp2/tools/fleet_sweep.py --record --hours 23.5

