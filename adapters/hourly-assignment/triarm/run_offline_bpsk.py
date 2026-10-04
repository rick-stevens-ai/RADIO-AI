#!/usr/bin/env python3
"""Assignment-bound BPSK qualification; structurally offline-only."""
import argparse, json, pathlib, subprocess, sys
from assignment import load_assignment

ROOT=pathlib.Path('/home/stevens/cw-propagation-inverse-20261003')
TEST='tests/test_waveforms.py::test_true_bpsk31_source_survives_channel_as_phase_sensitive_feed'

def main():
 p=argparse.ArgumentParser();p.add_argument('--assignment',type=pathlib.Path,required=True);p.add_argument('--dry-run',action='store_true');p.add_argument('--now-epoch',type=float);a=p.parse_args()
 assignment,file_sha=load_assignment(a.assignment,a.now_epoch)
 plan={'schema':'assignment-bound-offline-bpsk-v1','mode':'bpsk','selected_band':assignment['selected_band'],'assigned_receiver_ids':[x['receiver_id'] for x in assignment['assigned_sdrs']],'assignment_seal_sha256':assignment['seal']['digest'],'assignment_sha256':file_sha,'offline_only':True,'rf_capable':False,'rf_performed':False,'argv':[sys.executable,'-m','pytest','-q',TEST]}
 if a.dry_run: print(json.dumps(plan,sort_keys=True));return 0
 q=subprocess.run(plan['argv'],cwd=ROOT)
 return q.returncode
if __name__=='__main__':raise SystemExit(main())
