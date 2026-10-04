#!/usr/bin/env python3
"""Synchronized public-SDR capture around hash-bound AUDIO-CW/BFSK playback."""
import argparse,hashlib,json,os,pathlib,signal,subprocess,time,wave
from assignment import load_assignment
from carrier_plan import resolve_carrier
from receiver_assignment import assigned_receivers
ROOT=pathlib.Path(__file__).resolve().parent;SOURCE_ROOT=pathlib.Path('/home/stevens/radio/experiments/key10-20260927');KIWI='/home/stevens/sdr/kiwiclient/kiwirecorder.py'

def wait_stable(path,snapshot=None,pause=time.sleep,max_checks=20):
 snapshot=snapshot or (lambda:(path.stat().st_size,path.stat().st_mtime_ns));last=None
 for _ in range(max_checks):
  cur=snapshot()
  if cur==last:return cur
  last=cur;pause(.5)
 raise RuntimeError(f'capture did not stabilize: {path}')
def closeout_value(tx):return 'not_applicable' if tx is None else bool(tx.get('closeout') and all(v is False for k,v in tx['closeout'].items() if k in ('ptt','tx_enabled','sbkin','fbkin','rts','dtr')))
def stop(p):
 try:os.killpg(p.pid,signal.SIGINT);p.wait(timeout=5)
 except Exception:
  try:os.killpg(p.pid,signal.SIGKILL)
  except Exception:pass
def main():
 ap=argparse.ArgumentParser();ap.add_argument('arm',choices=['audio-cw','bfsk']);ap.add_argument('--assignment',type=pathlib.Path,required=True);ap.add_argument('--iteration',type=int,required=True);ap.add_argument('--carrier-hz',type=int);ap.add_argument('--wav',type=pathlib.Path);ap.add_argument('--manifest',type=pathlib.Path);ap.add_argument('--rfpower',type=float,default=.20);ap.add_argument('--capture-rehearsal',action='store_true');ap.add_argument('--dry-run',action='store_true');ap.add_argument('--now-epoch',type=float);a=ap.parse_args()
 assignment,assignment_sha=load_assignment(a.assignment,a.now_epoch);carrier=resolve_carrier(assignment['selected_band'],a.arm)
 if a.carrier_hz is not None and a.carrier_hz != carrier['carrier_hz']:raise SystemExit('--carrier-hz does not match assignment carrier')
 a.carrier_hz=carrier['carrier_hz'];rows=assigned_receivers(assignment)
 if not a.dry_run and (a.wav is None or a.manifest is None):raise SystemExit('--wav and --manifest are required outside dry-run')
 meta={} if a.manifest is None else json.load(open(a.manifest));sha=None if a.wav is None else hashlib.sha256(a.wav.read_bytes()).hexdigest()
 if a.wav is not None and sha!=meta.get('wav_sha256'):raise SystemExit('WAV hash mismatch')
 planned={'schema':'playback-sync-plan-v1','arm':a.arm,'iteration':a.iteration,'carrier_hz':a.carrier_hz,'sdr_dial_hz':carrier['sdr_dial_hz'],'selected_band':assignment['selected_band'],'assignment_sha256':assignment_sha,'assignment_seal_sha256':assignment['seal']['digest'],'assigned_receiver_ids':[r['candidate_id'] for r in rows],'message':meta.get('message') or meta.get('text'),'wpm':meta.get('wpm'),'wav_sha256':sha,'manifest_sha256':None if a.manifest is None else hashlib.sha256(a.manifest.read_bytes()).hexdigest(),'rfpower_fraction':a.rfpower,'rf_performed':False}
 if a.dry_run:print(json.dumps(planned));return 0
 contract=json.load(open(SOURCE_ROOT/'campaign-contract.json'));now=time.time()
 if now>=contract['authorization_deadline_epoch']-300:raise SystemExit('deadline reserve')
 cell=ROOT/'evidence'/f'triad-{a.iteration:04d}-{a.arm}';cell.mkdir(parents=True,exist_ok=False);procs={};launch=[];remote_result=None
 try:
  for r in rows:
   d=cell/'captures'/r['candidate_id'];d.mkdir(parents=True);cmd=['python3',KIWI,'-s',r['host'],'-p',str(r['port']),'-f',str((a.carrier_hz-1500)/1000),'-m','usb','-L','100','-H','3000','-r','12000','--tlimit','150','--fn',str(d/'capture'),'--connect-retries','1','--busy-retries','1','--log','warn'];ts=time.time();procs[r['candidate_id']]=subprocess.Popen(cmd,stdout=subprocess.DEVNULL,stderr=(d/'stderr.txt').open('w'),start_new_session=True);launch.append({'receiver_id':r['candidate_id'],'popen_epoch':ts})
  time.sleep(8);ready=[i for i in procs if list((cell/'captures'/i).glob('capture*.wav'))]
  if len(ready)<5:raise RuntimeError(f'capture quorum {len(ready)}')
  oq=subprocess.run(['python3',str(SOURCE_ROOT/'tools/check_capture_occupancy.py'),str(cell)],capture_output=True,text=True,timeout=30,check=True);occupancy=json.loads(oq.stdout);(cell/'occupancy.json').write_text(json.dumps(occupancy,indent=2)+'\n')
  if occupancy['clear']<5 or occupancy['busy']:raise RuntimeError(f'occupancy gate clear={occupancy["clear"]} busy={occupancy["busy"]}')
  local_cmd=f"R=/home/stevens/radio/agent/bin/radio;$R freq {a.carrier_hz};$R mode CW 500;$R cw --seconds 10 --method dsp;$R mode USB 3000;$R tx-disable"
  local=subprocess.run(['ssh','-n','rpi-gateway',local_cmd],capture_output=True,text=True,timeout=35);(cell/'local-clearance.txt').write_text(local.stdout+local.stderr)
  if local.returncode or 'no CW signal' not in local.stdout:raise RuntimeError('local occupancy gate')
  t0=time.time()+60;planned.update(t0_epoch=t0,ready_receivers=ready,capture_launches=launch)
  if a.capture_rehearsal:
   time.sleep(max(0,t0+float(meta['duration_s'])+10-time.time()));return finish(cell,planned,rows,procs,None)
  subprocess.run(['scp','-q',str(SOURCE_ROOT/'tools/audio_cw_remote_tx.py'),str(SOURCE_ROOT/'tools/telemetry-sampler'),str(SOURCE_ROOT/'tools/ld-c103-key-repeat.py'),str(a.wav),str(a.manifest),'rpi-gateway:/tmp/'],check=True,timeout=60)
  rw='/tmp/'+a.wav.name;rm='/tmp/'+a.manifest.name;receipt=f'/tmp/{a.arm}-receipt.json';remote=f"chmod 755 /tmp/audio_cw_remote_tx.py /tmp/telemetry-sampler /tmp/ld-c103-key-repeat.py; cp /tmp/ld-c103-key-repeat.py /tmp/ld-c103-key-retry; timeout --kill-after=3 180 python3 /tmp/audio_cw_remote_tx.py --carrier-hz {a.carrier_hz} --wav {rw} --manifest {rm} --start-epoch {t0:.6f} --rfpower {a.rfpower} --allow-tx --output {receipt}"
  remote_result=subprocess.run(['ssh','-n','rpi-gateway',remote],capture_output=True,text=True,timeout=200);(cell/'remote-result.json').write_text(json.dumps({'returncode':remote_result.returncode,'stdout':remote_result.stdout,'stderr':remote_result.stderr},indent=2)+'\n');subprocess.run(['scp','-q',f'rpi-gateway:{receipt}',str(cell/'tx.json')],check=False,timeout=30)
  if remote_result.returncode:raise RuntimeError(f'remote playback failed {remote_result.returncode}')
 finally:
  for p in procs.values():stop(p)
  subprocess.run(['ssh','-n','rpi-gateway','/home/stevens/radio/agent/bin/radio unkey >/dev/null 2>&1||true;/home/stevens/radio/agent/bin/radio tx-disable >/dev/null 2>&1||true;rigctl -m 2 -r 127.0.0.1:4532 set_func SBKIN 0 >/dev/null;rigctl -m 2 -r 127.0.0.1:4532 set_func FBKIN 0 >/dev/null'],timeout=30)
 return finish(cell,planned,rows,procs,remote_result)
def finish(cell,planned,rows,procs,remote_result):
 caps=[]
 for r in rows:
  for p in sorted((cell/'captures'/r['candidate_id']).glob('capture*.wav')):
   wait_stable(p)
   with wave.open(str(p),'rb') as w:dur=w.getnframes()/w.getframerate();facts={'channels':w.getnchannels(),'width':w.getsampwidth(),'rate':w.getframerate()}
   caps.append({'receiver_id':r['candidate_id'],'path':str(p.relative_to(cell)),'duration_s':dur,'facts':facts,'sha256':hashlib.sha256(p.read_bytes()).hexdigest()})
 tx=json.load(open(cell/'tx.json')) if (cell/'tx.json').exists() else None;manifest={**planned,'rf_performed':bool(tx and tx.get('sent')),'captures':caps,'tx':tx,'closeout_ok':closeout_value(tx)}
 (cell/'pair-manifest.json').write_text(json.dumps(manifest,indent=2)+'\n');files=sorted(p for p in cell.rglob('*') if p.is_file() and p.name!='SHA256SUMS');(cell/'SHA256SUMS').write_text(''.join(f'{hashlib.sha256(p.read_bytes()).hexdigest()}  {p.relative_to(cell)}\n' for p in files));print(json.dumps({'cell':str(cell),'arm':planned['arm'],'captures':len(caps),'rf_performed':manifest['rf_performed'],'closeout':manifest['closeout_ok']}));return 0 if ((not remote_result and len(caps)>=5) or (manifest['rf_performed'] and manifest['closeout_ok'] and len(caps)>=5)) else 2
if __name__=='__main__':raise SystemExit(main())
