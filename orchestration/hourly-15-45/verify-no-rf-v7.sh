#!/usr/bin/env bash
set -euo pipefail
HERE=$(cd -- "$(dirname -- "$0")" && pwd)
cd "$HERE"
sha256sum -c SHA256SUMS
(
 cd source/hourly-15-45
 PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src python3 -m pytest -q
 PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src python3 rehearse_overlap.py --scale .0003 --output /tmp/v7-reproduced-overlap.json
)
(
 cd source/ft8-scout
 PYTHONDONTWRITEBYTECODE=1 python3 -m pytest -q
)
(
 cd source/assignment-planner
 PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=. python3 -m pytest -q tests/test_assignment_adapters.py
)
python3 - <<'PY'
import json,pathlib
p=pathlib.Path('/tmp/v7-reproduced-overlap.json');d=json.load(open(p))
assert d['rf_performed'] is False
assert d['ft8_tx_count']==30 and d['ft8_rx_count']==30
assert len(d['rf_modes'])==7 and d['offline_modes']==['BPSK']
assert d['every_rf_interval_overlaps_processing'] and d['rf_lane_exclusive']
text='\n'.join(p.read_text() for p in pathlib.Path('source').rglob('*.py'))
for forbidden in ('enable_tx(', 'allow_tx=True', 'dry_run=False', "['ssh'", "['scp'"):
 assert forbidden not in text, forbidden
print('VERIFY_OK')
PY
