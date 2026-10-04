#!/usr/bin/env python3
"""Time-synchronized KEY TX / public-SDR capture pair."""
import argparse,hashlib,json,os,pathlib,signal,subprocess,time,wave
from assignment import load_assignment
from carrier_plan import resolve_carrier
from receiver_assignment import assigned_receivers
ROOT=pathlib.Path(__file__).resolve().parent
SOURCE_ROOT=pathlib.Path('/home/stevens/radio/experiments/key10-20260927')
KIWI='/home/stevens/sdr/kiwiclient/kiwirecorder.py'

def stop(p):
 try:os.killpg(p.pid,signal.SIGINT);p.wait(timeout=5)
 except Exception:
  try:os.killpg(p.pid,signal.SIGKILL)
  except Exception:pass
def finalize_control(cell,planned,rows,launch,procs):
 for p in procs.values():stop(p)
 caps=[]
 for r in rows:
  for p in sorted((cell/'captures'/r['candidate_id']).glob('capture*.wav')):
   with wave.open(str(p),'rb') as w:dur=w.getnframes()/w.getframerate();sr=w.getframerate()
   caps.append({'receiver_id':r['candidate_id'],'path':str(p.relative_to(cell)),'duration_s':dur,'sample_rate':sr,'file_mtime_epoch':p.stat().st_mtime,'inferred_start_epoch':p.stat().st_mtime-dur,'sha256':hashlib.sha256(p.read_bytes()).hexdigest()})
 tx={'sent':False,'rf_performed':False,'message':planned['message'],'wpm':planned['wpm'],'repeat':planned['repeat'],'source_schedule':planned['source_schedule']}
 manifest={**planned,'capture_launches':launch,'captures':caps,'tx':tx,'closeout_ok':True}
 (cell/'pair-manifest.json').write_text(json.dumps(manifest,indent=2)+'\n')
 files=sorted(p for p in cell.rglob('*') if p.is_file() and p.name!='SHA256SUMS');(cell/'SHA256SUMS').write_text(''.join(f'{hashlib.sha256(p.read_bytes()).hexdigest()}  {p.relative_to(cell)}\n' for p in files))
 print(json.dumps({'cell':str(cell),'control':True,'captures':len(caps),'closeout':True},indent=2));return 0 if len(caps)>=5 else 2

def main():
 ap=argparse.ArgumentParser();ap.add_argument('--assignment',type=pathlib.Path,required=True);ap.add_argument('--iteration',type=int,required=True);ap.add_argument('--frequency-hz',type=int);ap.add_argument('--wpm',type=int,default=18);ap.add_argument('--repeat',type=int,default=3);ap.add_argument('--message',default='DE KD9NWA R1');ap.add_argument('--capture-rehearsal',action='store_true');ap.add_argument('--no-tx',action='store_true');ap.add_argument('--dry-run',action='store_true');ap.add_argument('--now-epoch',type=float);a=ap.parse_args()
 assignment,assignment_sha=load_assignment(a.assignment,a.now_epoch);carrier=resolve_carrier(assignment['selected_band'],'key-cw')
 if a.frequency_hz is not None and a.frequency_hz != carrier['carrier_hz']:raise SystemExit('--frequency-hz does not match assignment carrier')
 a.frequency_hz=carrier['carrier_hz']
 rows=assigned_receivers(assignment);now=time.time()
 if not a.message or len(a.message)>60 or any(ch not in 'ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789 /' for ch in a.message):raise SystemExit('unsafe/unsupported message')
 t0=now+15;planned={'iteration':a.iteration,'frequency_hz':a.frequency_hz,'sdr_dial_hz':carrier['sdr_dial_hz'],'selected_band':assignment['selected_band'],'assignment_sha256':assignment_sha,'assignment_seal_sha256':assignment['seal']['digest'],'assigned_receiver_ids':[r['candidate_id'] for r in rows],'t0_epoch':t0,'capture_lead_s':10,'capture_duration_s':150,'wpm':a.wpm,'repeat':a.repeat,'message':a.message,'receivers':[r['candidate_id'] for r in rows],'rf_performed':False}
 if a.dry_run:print(json.dumps(planned,indent=2));return 0
 contract=json.load(open(SOURCE_ROOT/'campaign-contract.json'))
 if now>=contract['authorization_deadline_epoch']-180:raise SystemExit('deadline reserve')
 cell=ROOT/'evidence'/f'epoch-{a.iteration:02d}-key-sync';cell.mkdir(parents=True,exist_ok=False);procs={};launch=[]
 try:
  for r in rows:
   d=cell/'captures'/r['candidate_id'];d.mkdir(parents=True);base=d/'capture';cmd=['python3',KIWI,'-s',r['host'],'-p',str(r['port']),'-f',str((a.frequency_hz-1500)/1000),'-m','usb','-L','100','-H','3000','-r','12000','--tlimit','150','--fn',str(base),'--connect-retries','1','--busy-retries','1','--log','warn'];ts=time.time();procs[r['candidate_id']]=subprocess.Popen(cmd,stdout=subprocess.DEVNULL,stderr=(d/'stderr.txt').open('w'),start_new_session=True);launch.append({'receiver_id':r['candidate_id'],'popen_epoch':ts})
  time.sleep(8);ready=[i for i,p in procs.items() if list((cell/'captures'/i).glob('capture*.wav'))]
  if len(ready)<5:raise RuntimeError(f'capture quorum {len(ready)}')
  oq=subprocess.run(['python3',str(SOURCE_ROOT/'tools/check_capture_occupancy.py'),str(cell)],capture_output=True,text=True,timeout=30)
  if oq.returncode:raise RuntimeError('occupancy helper failed')
  occupancy=json.loads(oq.stdout);(cell/'occupancy.json').write_text(json.dumps(occupancy,indent=2)+'\n')
  if not a.no_tx and (occupancy['clear']<5 or occupancy['busy']):raise RuntimeError(f'occupancy gate clear={occupancy["clear"]} busy={occupancy["busy"]}')
  if not a.no_tx:
   local=subprocess.run(['ssh','-n','rpi-gateway','/home/stevens/radio/agent/bin/radio cw --seconds 10 --method dsp'],capture_output=True,text=True,timeout=30)
   (cell/'local-clearance.json').write_text(local.stdout if local.stdout.strip() else json.dumps({'error':local.stderr}))
   if local.returncode or ('no CW signal' not in local.stdout):raise RuntimeError('local occupancy gate')
  t0=time.time()+60
  planned['t0_epoch']=t0
  planned['ready_epoch']=time.time()
  if a.no_tx:
   unit=1.2/a.wpm;words=a.message.split();copies=[];cursor=t0
   for ci in range(a.repeat):
    events=[];copy_start=cursor
    for wi,word in enumerate(words):
     for xi,ch in enumerate(word):
      for mi,mark in enumerate(__import__('runpy').run_path(str(SOURCE_ROOT/'tools/ld-c103-key-repeat.py'))['MORSE'][ch]):
       down=unit if mark=='.' else 3*unit;events.append({'char':ch,'mark':mark,'started_epoch':cursor,'finished_epoch':cursor+down});cursor+=down
       if mi<len(__import__('runpy').run_path(str(SOURCE_ROOT/'tools/ld-c103-key-repeat.py'))['MORSE'][ch])-1:cursor+=unit
      if xi<len(word)-1:cursor+=3*unit
     if wi<len(words)-1:cursor+=7*unit
    copies.append({'copy':ci+1,'started_epoch':copy_start,'finished_epoch':cursor,'events':events})
    if ci+1<a.repeat:cursor+=5
   time.sleep(max(0,cursor+10-time.time()))
   planned.update({'ready_epoch':time.time(),'ready_receivers':ready,'rf_performed':False,'control_type':'matched-no-tx','source_schedule':{'text':a.message,'wpm':a.wpm,'unit_s':unit,'repeat':a.repeat,'started_epoch':t0,'finished_epoch':cursor,'copies':copies}})
   return finalize_control(cell,planned,rows,launch,procs)
  if a.capture_rehearsal:
   planned['assigned_lead_s']=t0-planned['ready_epoch']
   planned['ready_receivers']=ready
   planned['rf_performed']=False
   (cell/'rehearsal-manifest.json').write_text(json.dumps(planned,indent=2)+'\n')
   print(json.dumps(planned,indent=2));return 0
  subprocess.run(['scp','-q',str(SOURCE_ROOT/'tools/key10_remote_tx_rust.py'),str(SOURCE_ROOT/'tools/ld-c103-key-repeat.py'),str(SOURCE_ROOT/'tools/telemetry-sampler'),'rpi-gateway:/tmp/'],check=True)
  remote=f"chmod 755 /tmp/key10_remote_tx_rust.py /tmp/ld-c103-key-repeat.py /tmp/telemetry-sampler; cp /tmp/ld-c103-key-repeat.py /tmp/key10-keyer; rm -f /tmp/key10-tx.json; trap '/home/stevens/radio/agent/bin/force-safe-closeout >/dev/null 2>&1 || true' EXIT HUP INT TERM; timeout --kill-after=3 140 python3 /tmp/key10_remote_tx_rust.py --frequency-hz {a.frequency_hz} --message '{a.message}' --wpm {a.wpm} --repeat {a.repeat} --start-epoch {t0:.6f}"
  txlaunch=time.time();remote_result=subprocess.run(['ssh','-n','rpi-gateway',remote],capture_output=True,text=True,timeout=160);(cell/'remote-result.json').write_text(json.dumps({'returncode':remote_result.returncode,'stdout':remote_result.stdout,'stderr':remote_result.stderr},indent=2)+'\n');subprocess.run(['scp','-q','rpi-gateway:/tmp/key10-tx.json',str(cell/'tx.json')],timeout=30,check=False);
  if remote_result.returncode:raise RuntimeError(f'remote TX failed rc={remote_result.returncode}')
 finally:
  for p in procs.values():stop(p)
  clean=f"R=/home/stevens/radio/agent/bin/radio; /home/stevens/radio/agent/bin/force-safe-closeout;$R freq {a.frequency_hz} >/dev/null;$R mode USB 3000 >/dev/null;$R status;$R tx-status;/tmp/key10-keyer status"
  q=subprocess.run(['ssh','-n','rpi-gateway',clean],capture_output=True,text=True,timeout=40);(cell/'closeout.txt').write_text(q.stdout+q.stderr)
 tx=json.load(open(cell/'tx.json'));caps=[]
 for r in rows:
  ws=sorted((cell/'captures'/r['candidate_id']).glob('capture*.wav'))
  for p in ws:
   with wave.open(str(p),'rb') as w:dur=w.getnframes()/w.getframerate();sr=w.getframerate()
   caps.append({'receiver_id':r['candidate_id'],'path':str(p.relative_to(cell)),'duration_s':dur,'sample_rate':sr,'file_mtime_epoch':p.stat().st_mtime,'inferred_start_epoch':p.stat().st_mtime-dur,'sha256':hashlib.sha256(p.read_bytes()).hexdigest()})
 manifest={**planned,'rf_performed':True,'tx_launch_epoch':txlaunch,'actual_key_start_epoch':tx['source_schedule']['started_epoch'],'start_error_s':tx['source_schedule']['started_epoch']-t0,'capture_launches':launch,'captures':caps,'tx':tx,'closeout_ok':'"ptt": false' in q.stdout and '"tx_enabled": false' in q.stdout}
 (cell/'pair-manifest.json').write_text(json.dumps(manifest,indent=2)+'\n');files=sorted(p for p in cell.rglob('*') if p.is_file() and p.name!='SHA256SUMS');(cell/'SHA256SUMS').write_text(''.join(f'{hashlib.sha256(p.read_bytes()).hexdigest()}  {p.relative_to(cell)}\n' for p in files));print(json.dumps({'cell':str(cell),'t0':t0,'start_error_s':manifest['start_error_s'],'captures':len(caps),'closeout':manifest['closeout_ok']},indent=2));return 0 if manifest['closeout_ok'] and len(caps)>=5 else 2
if __name__=='__main__':raise SystemExit(main())
