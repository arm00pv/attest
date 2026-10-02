#!/bin/bash
# Daily coverage check: did every scheduled run actually produce a result?
FLEET_REQUIRE_DB=/home/zixen15/.attest-ledger/attest.db exec /home/zixen15/attest-mcp2/bin/fleet_run.sh coverage 180 \
  /usr/bin/python3 /home/zixen15/attest-mcp2/tools/fleet_coverage.py

