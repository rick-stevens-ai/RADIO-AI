#!/usr/bin/env python3
import pathlib,sys,time,run_hourly
base=pathlib.Path('/tmp/ft8-propagation-full-rehearsal')
run_hourly.STATE=base/'state.json'
run_hourly.RUNS=base/'runs'
run_hourly.LOCK=base/'rf.lock'
sys.argv=['run_hourly.py','--deadline-epoch',str(time.time()+1200),'--window-s','900','--no-rf']
raise SystemExit(run_hourly.main())
