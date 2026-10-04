#!/usr/bin/env bash
set -euo pipefail
HERE=$(cd -- "$(dirname -- "$0")" && pwd)
cd "$HERE"
sha256sum -c SHA256SUMS
(
 cd source/hourly-15-45
 PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src python3 -m pytest -q
 PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src python3 rehearse_overlap.py --scale .0003 --output "$HERE/reproduced-overlap.json"
)
(
 cd source/ft8-scout
 PYTHONDONTWRITEBYTECODE=1 python3 -m pytest -q
)
(
 cd source/hourly-15-45-adapters
 PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=. python3 -m pytest -q tests/test_assignment_adapters.py
 PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=executors python3 -m pytest -q executors/tests/test_run_application_cell_assignment.py
 PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=triarm python3 -m pytest -q triarm/tests/test_assignment_consumption.py
)
python3 - <<'PY'
import json,pathlib
p=pathlib.Path('reproduced-overlap.json');d=json.load(open(p))
assert d['rf_performed'] is False
assert d['ft8_tx_count']==30 and d['ft8_rx_count']==30
assert len(d['rf_modes'])==7 and d['offline_modes']==['BPSK']
assert d['every_rf_interval_overlaps_processing'] and d['rf_lane_exclusive']
print('VERIFY_OK')
PY
