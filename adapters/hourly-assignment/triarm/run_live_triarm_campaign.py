#!/usr/bin/env python3
"""Durable serialized KEY/AUDIO-CW/BFSK matched campaign."""
import argparse,hashlib,json,math,os,pathlib,secrets,string,subprocess,sys,time
from assignment import load_assignment
from carrier_plan import resolve_carrier
from receiver_assignment import assigned_receivers
ROOT=pathlib.Path(__file__).resolve().parent
SOURCE_ROOT=pathlib.Path('/home/stevens/radio/experiments/key10-20260927')
ARMS=(('key','audio-cw','bfsk'),('audio-cw','bfsk','key'),('bfsk','key','audio-cw'))
WPMS=(12,)
def atomic(path,obj):
 path.parent.mkdir(parents=True,exist_ok=True);tmp=path.with_suffix('.tmp');tmp.write_text(json.dumps(obj,indent=2,sort_keys=True)+'\n');os.replace(tmp,path)
def sha(p):return hashlib.sha256(p.read_bytes()).hexdigest()
def run(cmd,timeout):return subprocess.run(cmd,text=True,capture_output=True,timeout=timeout)
def generate(triad,msg,wpm,carriers):
 d=ROOT/'tri-arm-live'/f'triad-{triad:04d}';d.mkdir(parents=True,exist_ok=False);rep=2 if wpm<=12 else 3
 q=run(['python3',str(SOURCE_ROOT/'tools/generate_audio_cw.py'),'--message',msg,'--wpm',str(wpm),'--repeat',str(rep),'--output',str(d/'audio-cw.wav')],60)
 if q.returncode:raise RuntimeError('audio fixture generation failed: '+q.stderr)
 q=run(['python3',str(SOURCE_ROOT/'tools/bfsk_cw.py'),'generate','--text',msg,'--output',str(d/'bfsk.wav')],60)
 if q.returncode:raise RuntimeError('BFSK fixture generation failed: '+q.stderr)
 carrier=carriers['bfsk'];b=json.loads(q.stdout);b.update(schema='hash-bound-playback-v1',kind='bfsk',wav_sha256=sha(d/'bfsk.wav'),dial_hz=carrier['sdr_dial_hz'],carrier_hz=carrier['carrier_hz'],message=msg);atomic(d/'bfsk.json',b)
 return d,rep
def cell_path(triad,arm):return ROOT/'evidence'/(f'epoch-{20000+triad}-key-sync' if arm=='key' else f'triad-{20000+triad}-{arm}')
def classify(path):
 tx=path/'tx.json'
 if not tx.exists():return 'blocked-pre-rf',None
 d=json.load(open(tx));return ('finished' if d.get('sent') else 'failed-rf'),d
def arm_command(triad,arm,d,msg,wpm,rep,assignment):
 iteration=20000+triad
 if arm=='key':return ['python3',str(ROOT/'run_sync_pair.py'),'--assignment',str(assignment),'--iteration',str(iteration),'--wpm',str(wpm),'--repeat',str(rep),'--message',msg]
 return ['python3',str(ROOT/'run_playback_sync_pair.py'),arm,'--assignment',str(assignment),'--iteration',str(iteration),'--wav',str(d/(arm+'.wav')),'--manifest',str(d/(arm+'.json')),'--rfpower','.20']
def safety_block(state_path):
 p=pathlib.Path(state_path).parent.parent/'triarm-safety-block.json'
 return json.load(open(p)) if p.exists() else None
def main():
 ap=argparse.ArgumentParser();ap.add_argument('--assignment',type=pathlib.Path,required=True);ap.add_argument('--start-triad',type=int,default=10);ap.add_argument('--max-triads',type=int,default=150);ap.add_argument('--deadline-epoch',type=float);ap.add_argument('--state',type=pathlib.Path,default=ROOT/'runtime/triarm-live-state.json');ap.add_argument('--dry-run',action='store_true');ap.add_argument('--now-epoch',type=float);a=ap.parse_args()
 assignment,assignment_sha=load_assignment(a.assignment,a.now_epoch)
 if a.deadline_epoch is not None and a.deadline_epoch != assignment['expires_epoch']:raise SystemExit('--deadline-epoch must match assignment expiry')
 a.deadline_epoch=assignment['expires_epoch'];receivers=assigned_receivers(assignment);carriers={mode:resolve_carrier(assignment['selected_band'],mode) for mode in ('key-cw','audio-cw','bfsk')}
 if a.dry_run:
  print(json.dumps({'schema':'assignment-bound-triarm-plan-v1','run_id':assignment['run_id'],'selected_band':assignment['selected_band'],'assignment_sha256':assignment_sha,'assignment_seal_sha256':assignment['seal']['digest'],'assigned_receiver_ids':[r['candidate_id'] for r in receivers],'carriers':carriers,'start_triad':a.start_triad,'max_triads':a.max_triads,'rf_performed':False},sort_keys=True));return 0
 state=json.load(open(a.state)) if a.state.exists() else {'schema':'triarm-live-state-v1','deadline_epoch':a.deadline_epoch,'triads':[]}
 if state.get('deadline_epoch')!=a.deadline_epoch:raise SystemExit('state deadline mismatch')
 for offset in range(a.max_triads):
  triad=a.start_triad+offset
  if any(x['triad']==triad for x in state['triads']):continue
  if time.time()+900>=a.deadline_epoch:state['status']='deadline';atomic(a.state,state);break
  carrier=carriers['key-cw']['carrier_hz'];wpm=WPMS[offset%len(WPMS)];nonce=''.join(secrets.choice(string.ascii_uppercase+string.digits) for _ in range(3));msg='DE KD9NWA '+nonce;order=list(ARMS[(triad-1)%3])
  row={'triad':triad,'phase':'reserved-no-replay','reserved_epoch':time.time(),'selected_band':assignment['selected_band'],'carrier_hz':carrier,'assignment_sha256':assignment_sha,'assigned_receiver_ids':[r['candidate_id'] for r in receivers],'wpm':wpm,'nonce':nonce,'message':msg,'order':order,'arms':[]};state['triads'].append(row);atomic(a.state,state)
  try:d,rep=generate(triad,msg,wpm,carriers);row['repeat']=rep;atomic(d/'plan.json',row)
  except Exception as e:row.update(phase='failed-build',error=repr(e));atomic(a.state,state);break
  hard=False
  for arm in order:
   p=cell_path(triad,arm);q=run(arm_command(triad,arm,d,msg,wpm,rep,a.assignment),300);kind,tx=classify(p);ar={'arm':arm,'phase':kind,'returncode':q.returncode,'stdout':q.stdout,'stderr':q.stderr,'path':str(p),'finished_epoch':time.time()};row['arms'].append(ar);atomic(a.state,state)
   if kind=='blocked-pre-rf':row.update(phase='blocked-pre-rf',finished_epoch=time.time());atomic(a.state,state);hard=True;break
   if kind!='finished':row.update(phase='failed-rf',finished_epoch=time.time());atomic(a.state,state);hard=True;break
   active=float(tx.get('rf_active_duration_s',tx.get('duration_s',31.6)));time.sleep(max(30,math.ceil(.5*active)))
  if hard:
   # Safety/RF failures stop; occupancy blockers continue on next candidate.
   if row['phase']=='failed-rf':state['status']='stopped-hard-failure';atomic(a.state,state);break
   continue
  row.update(phase='finished',finished_epoch=time.time());atomic(a.state,state)
 else:state['status']='cycle-cap';atomic(a.state,state)
 return 0
if __name__=='__main__':raise SystemExit(main())
