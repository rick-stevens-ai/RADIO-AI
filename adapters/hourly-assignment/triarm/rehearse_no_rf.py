#!/usr/bin/env python3
"""Execute every assignment consumer in side-effect-free dry-run mode."""
import argparse, json, pathlib, subprocess, sys
from assignment import load_assignment

ROOT=pathlib.Path(__file__).resolve().parent

def invoke(args):
 q=subprocess.run([sys.executable,*map(str,args)],text=True,capture_output=True)
 if q.returncode: raise SystemExit(q.stderr or f"rehearsal failed: {args}")
 return json.loads(q.stdout)

def main():
 p=argparse.ArgumentParser();p.add_argument('--assignment',type=pathlib.Path,required=True);p.add_argument('--now-epoch',type=float);a=p.parse_args()
 assignment,file_sha=load_assignment(a.assignment,a.now_epoch);common=['--assignment',a.assignment,'--dry-run'];
 if a.now_epoch is not None: common += ['--now-epoch',str(a.now_epoch)]
 rows=[invoke([ROOT/'run_live_triarm_campaign.py',*common,'--start-triad','1','--max-triads','1']),invoke([ROOT/'run_sync_pair.py',*common,'--iteration','1']),invoke([ROOT/'run_playback_sync_pair.py','audio-cw',*common,'--iteration','1']),invoke([ROOT/'run_playback_sync_pair.py','bfsk',*common,'--iteration','1']),invoke([ROOT/'run_offline_bpsk.py',*common])]
 assert all(row.get('rf_performed') is False for row in rows)
 print(json.dumps({'schema':'triarm-assignment-no-rf-rehearsal-v1','assignment_sha256':file_sha,'selected_band':assignment['selected_band'],'commands':rows,'rf_performed':False},indent=2,sort_keys=True))
 return 0
if __name__=='__main__':raise SystemExit(main())
