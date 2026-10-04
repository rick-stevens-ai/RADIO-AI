#!/usr/bin/env python3
"""Hourly 15-minute FT8 propagation-admission loop.

One immutable hour transaction:
  9-band retained-SDR FT8 sweep -> deterministic safe band/lane selection ->
  reporter-fallback SDR setup -> one validation beacon -> exact UTC-segment
  retained-WAV decode -> delayed PSKReporter confirmation -> next-hour roster
  and TTL decision.
"""
from __future__ import annotations
import argparse,concurrent.futures,datetime,fcntl,hashlib,importlib.util,json,math,os,pathlib,signal,subprocess,sys,tempfile,time,urllib.parse,urllib.request,uuid,wave,xml.etree.ElementTree as ET

HERE=pathlib.Path(__file__).resolve().parent
STATE=HERE/'state.json';RUNS=HERE/'runs';LOCK=pathlib.Path('/home/stevens/radio/rf-campaign.lock')
KIWI='/home/stevens/sdr/kiwiclient/kiwirecorder.py';RADIO='/home/stevens/radio/agent/bin/radio';REMOTE_PY='/home/stevens/radio/cwprop-adapter';REMOTE_TX='/home/stevens/radio/propagation-admission/ft8_beacon.py'
SWEEP=HERE/'sweep_ft8.py';LOCATOR=pathlib.Path('/home/stevens/weft-open-placement-agent/tools/locate_sdrs_from_pskreporter.py')
RECEIVERBOOK='https://www.receiverbook.de/map?type=kiwisdr';PSK='https://retrieve.pskreporter.info/query'
FT8_DIAL={'80m':3573000,'60m':5357000,'40m':7074000,'30m':10136000,'20m':14074000,'17m':18100000,'15m':21074000,'12m':24915000,'10m':28074000}
TX_ELIGIBLE={'80m','40m','30m','20m','15m','12m','10m'};DEFAULT_BLOCKED={'17m'};DIAL_RANGE={b:(hz-10000,hz+10000) for b,hz in FT8_DIAL.items()}
MESSAGE='CQ KD9NWA EN51';CALL='KD9NWA';GRID='EN51TP';MIN_QUORUM=2
LANE_CLEAR_DB=15.0

def sweep_module():
 spec=importlib.util.spec_from_file_location('sweep_ft8_runtime',SWEEP);assert spec and spec.loader
 module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module);return module

def site_key(row,unknown_host_fallback=False):
 lat,lon=row.get('lat'),row.get('lon')
 if lat is not None and lon is not None:return ('ll',round(float(lat),2),round(float(lon),2))
 if row.get('site_id'):return ('id',str(row['site_id']).strip().lower())
 return ('host',str(row.get('host','')).strip().lower()) if unknown_host_fallback and row.get('host') else None

def merge_unique_sdrs(*groups_and_limit):
 *groups,limit=groups_and_limit;out=[];endpoints=set();sites=set()
 for group in groups:
  for source in group:
   s=dict(source.get('sdr',source));endpoint=(str(s['host']).lower(),int(s['port']));site=site_key(s,True)
   if endpoint in endpoints or site in sites:continue
   endpoints.add(endpoint);sites.add(site);out.append(s)
   if len(out)>=limit:return out
 return out

def pre_tx_clearance(remote_rows,local_row,minimum=MIN_QUORUM):
 sites={site_key(x,True) for x in remote_rows if x.get('available') and x.get('clear')}
 return len(sites)>=minimum and bool(local_row and local_row.get('available') and local_row.get('clear'))

def admission_result(validation_rows,reports,now=None):
 now=time.time() if now is None else now;exact=[x for x in validation_rows if x.get('status')=='exact']
 exact_sites={site_key(x) for x in exact if site_key(x) is not None};result={'status':'validated' if len(exact_sites)>=MIN_QUORUM else 'not-validated','exact_receiver_count':len(exact),'exact_site_count':len(exact_sites),'psk_report_count':len(reports)}
 if result['status']=='validated':result['valid_until_epoch']=now+3600
 return result

def hour_key(epoch):return datetime.datetime.fromtimestamp(epoch,datetime.timezone.utc).strftime('%Y%m%dT%H')
def ranked_bands(rows,blocked=None):
 blocked=set(blocked or DEFAULT_BLOCKED);usable=[x for x in rows if x['band'] in TX_ELIGIBLE-blocked]
 return sorted(usable,key=lambda x:(x.get('unique_calls',0),x.get('n_decodes',0),x.get('clear_receivers',0),x.get('best_snr_db',-99)),reverse=True)
def choose_band(rows,blocked=None):
 usable=ranked_bands(rows,blocked)
 if not usable:raise RuntimeError('no TX-eligible band')
 return usable[0]
def filter_reports(rows,band,tx_epoch):
 lo,hi=DIAL_RANGE[band];end=float(tx_epoch)+15
 return [x for x in rows if x.get('mode')=='FT8' and lo<=int(x.get('freq_hz',0))<=hi and float(tx_epoch)<=int(x.get('flow_start',0))<end]
def may_start_hour(state,key):return all(x.get('key')!=key for x in state.get('hours',[]))
def choose_offset(occupied):
 for x in (1200,1500,1800,900,2100,2400,600):
  if all(abs(x-y)>=120 for y in occupied):return x
 raise RuntimeError('no clear FT8 audio lane')
def choose_clear_offset(rows,occupied,minimum=MIN_QUORUM):
 for offset in (1200,1500,1800,900,2100,2400,600):
  if any(abs(offset-y)<120 for y in occupied):continue
  clear=[x for x in rows if x.get('available') and float(x.get('lane_metrics_db',{}).get(str(offset),math.inf))<LANE_CLEAR_DB]
  if len({site_key(x,True) for x in clear})>=minimum:return offset,clear
 raise RuntimeError('no FT8 audio lane has remote clearance quorum')
def unique_sdrs(ranked,limit):
 return merge_unique_sdrs(ranked,limit)

def atomic(path,obj):
 atomic_text(path,json.dumps(obj,indent=2,sort_keys=True)+'\n')
def atomic_text(path,text):
 path.parent.mkdir(parents=True,exist_ok=True)
 fd,name=tempfile.mkstemp(prefix=f'.{path.name}.',suffix='.tmp',dir=path.parent)
 try:
  with os.fdopen(fd,'w') as f:
   f.write(text);f.flush();os.fsync(f.fileno())
  os.replace(name,path)
  dfd=os.open(path.parent,os.O_RDONLY|os.O_DIRECTORY)
  try:os.fsync(dfd)
  finally:os.close(dfd)
 finally:
  try:os.unlink(name)
  except FileNotFoundError:pass
def digest(path):return hashlib.sha256(path.read_bytes()).hexdigest()
def sh(cmd,timeout=120,cwd=None):return subprocess.run(cmd,text=True,capture_output=True,timeout=timeout,cwd=cwd)
def require_time(deadline,needed=0,now=None,cap=120):
 now=time.time() if now is None else now
 if now+needed>=deadline:raise RuntimeError('global deadline would be exceeded')
 return max(1,min(cap,int(deadline-now-needed)))
def stop(proc):
 try:os.killpg(proc.pid,signal.SIGINT);proc.wait(timeout=5)
 except Exception:
  try:os.killpg(proc.pid,signal.SIGKILL)
  except Exception:pass
def stop_all(procs):
 for proc in list(procs.values()):
  try:stop(proc)
  except Exception:pass
 procs.clear()

def station_snapshot():
 q=sh(['ssh','-n','rpi-gateway',RADIO,'status'],30)
 if q.returncode:raise RuntimeError(f'station snapshot failed: {q.stderr}')
 return json.loads(q.stdout)
def restore_station(snapshot):
 script=(f"R={RADIO};$R unkey >/dev/null 2>&1||true;$R tx-disable >/dev/null 2>&1||true;"
         f"$R freq {int(snapshot['freq_hz'])} >/dev/null;$R mode {snapshot['mode']} {int(snapshot['passband_hz'])} >/dev/null;$R status")
 q=sh(['ssh','-n','rpi-gateway',script],40)
 if q.returncode:return {'safe':False,'restored':False,'error':q.stderr}
 try:now=json.loads(q.stdout)
 except Exception:return {'safe':False,'restored':False,'error':'invalid status readback'}
 restored=now.get('freq_hz')==snapshot.get('freq_hz') and now.get('mode')==snapshot.get('mode') and now.get('passband_hz')==snapshot.get('passband_hz')
 return {'safe':now.get('ptt') is False,'restored':restored,'readback':now}

def process_collision():
 needles=('run_application_cell.py','run_live_triarm_campaign.py','key10_remote_tx','audio_cw_remote_tx','ft8_beacon.py','wsjtx','js8call','fldigi')
 local=sh(['ps','-eo','args'],20);remote=sh(['ssh','-n','rpi-gateway','ps -eo args'],20)
 rows=[f'local:{line}' for line in local.stdout.splitlines()]+[f'remote:{line}' for line in remote.stdout.splitlines()]
 return [line for line in rows if any(n in line.lower() for n in needles) and 'run_hourly.py' not in line and 'grep ' not in line]
def fetch(url,timeout=45):
 req=urllib.request.Request(url,headers={'User-Agent':'KD9NWA-propagation-admission/1.0'});return urllib.request.urlopen(req,timeout=timeout).read()
def locator_module():
 spec=importlib.util.spec_from_file_location('locator',LOCATOR);assert spec and spec.loader;m=importlib.util.module_from_spec(spec);spec.loader.exec_module(m);return m

def parse_psk(raw):
 rows=[]
 for n in ET.fromstring(raw).iter('receptionReport'):
  a=n.attrib
  try:freq=int(float(a.get('frequency',0)));flow=int(a.get('flowStartSeconds',0));snr=int(float(a.get('sNR',0)))
  except ValueError:continue
  rows.append({'receiver':a.get('receiverCallsign',''),'grid':a.get('receiverLocator',''),'snr_db':snr,'freq_hz':freq,'mode':a.get('mode',''),'flow_start':flow})
 return rows

def decode_wav(path,timeout=60):
 path=pathlib.Path(path).resolve()
 with tempfile.TemporaryDirectory(prefix='jt9-isolated-') as td:q=sh(['/usr/bin/jt9','-8','-d','3','-a',td,'-t',td,str(path)],timeout,td)
 rows=[]
 for line in q.stdout.splitlines():
  z=line.split(maxsplit=5)
  if len(z)>=6 and z[0].isdigit():
   try:rows.append({'utc':z[0],'snr_db':int(z[1]),'dt_s':float(z[2]),'offset_hz':int(z[3]),'message':z[5].strip()})
   except ValueError:pass
 return rows

def kiwi_capture_start(path):
 name=pathlib.Path(path).name
 try:stamp=name.split('_',1)[0];dt=datetime.datetime.strptime(stamp,'%Y%m%dT%H%M%SZ').replace(tzinfo=datetime.timezone.utc);return dt.timestamp()
 except Exception as e:raise ValueError(f'Kiwi filename lacks sample-zero UTC timestamp: {name}') from e

def select_kiwi_slot(paths,slot_epoch):
 exact=[pathlib.Path(p) for p in paths if abs(kiwi_capture_start(p)-float(slot_epoch))<0.5]
 if len(exact)!=1:raise ValueError(f'exact Kiwi segment not found for slot {slot_epoch}')
 return exact[0]

def extract_tx_slot(source,destination,capture_meta,slot_epoch):
 if 'capture_start_epoch' not in capture_meta or 'sample_rate' not in capture_meta:raise ValueError('explicit capture timing metadata required')
 with wave.open(str(source),'rb') as src:
  channels,width,rate,frames=src.getnchannels(),src.getsampwidth(),src.getframerate(),src.getnframes()
  if channels!=1 or width!=2 or rate!=12000:raise ValueError('not mono PCM16 12 kHz')
  start=round((slot_epoch-float(capture_meta['capture_start_epoch']))*rate);count=15*rate
  if start<0 or start+count>frames:raise ValueError('transmitted slot not fully contained in capture')
  src.setpos(start);raw=src.readframes(count)
 with wave.open(str(destination),'wb') as dst:dst.setnchannels(1);dst.setsampwidth(2);dst.setframerate(rate);dst.writeframes(raw)
 return {'capture_start_epoch':capture_meta['capture_start_epoch'],'slot_epoch':slot_epoch,'start_frame':start,'frames':count,'duration_s':15.0,'sample_rate':rate}

def local_clearance_capture(root,dial,offset,seconds=15):
 """Capture station audio on rpi-gateway receive-only and restore CAT state."""
 path=root/'local-clearance.wav';root.mkdir(parents=True,exist_ok=True);remote=f'/tmp/ft8-local-clear-{uuid.uuid4().hex}.wav'
 script=(f"set -Eeuo pipefail;R={RADIO};orig_f=$($R status|python3 -c 'import json,sys;print(json.load(sys.stdin)[\"freq_hz\"])');"
         f"orig_m=$($R status|python3 -c 'import json,sys;x=json.load(sys.stdin);print(x[\"mode\"],x[\"passband_hz\"])');"
         f"cleanup() {{ $R unkey >/dev/null 2>&1||true;$R tx-disable >/dev/null 2>&1||true;set -- $orig_m;$R freq $orig_f >/dev/null 2>&1||true;$R mode $1 $2 >/dev/null 2>&1||true; }};trap cleanup EXIT INT TERM HUP;"
         f"$R tx-disable >/dev/null;$R unkey >/dev/null;$R freq {dial} >/dev/null;$R mode PKTUSB 3000 >/dev/null;"
         f"PYTHONPATH=/home/stevens/radio/cwprop-adapter python3 -c 'from hamradio.audio import record_wav;record_wav({seconds},out_path=\"{remote}\",rate=12000)'")
 q=sh(['ssh','-n','rpi-gateway',script],seconds+35)
 if q.returncode:raise RuntimeError(f'local receive-only capture failed: {q.stderr}')
 q=sh(['scp','-q',f'rpi-gateway:{remote}',str(path)],30)
 if q.returncode:raise RuntimeError(f'local clearance copy failed: {q.stderr}')
 sweep=sweep_module();info=sweep.wav_info(path);metric=sweep.lane_metric(path,offset)
 return {**info,'wav':str(path),'available':info['duration_s']>=13,'clear':metric<LANE_CLEAR_DB,'lane_metric_db':metric,'receive_only':True}

def start_captures(root,roster,dial):
 root.mkdir(parents=True,exist_ok=True);procs={};started={}
 try:
  for i,r in enumerate(roster):
   rid=r.get('candidate_id') or f'sdr-{i+1:02d}';r['candidate_id']=rid;d=root/rid;d.mkdir();cmd=['python3',KIWI,'-s',r['host'],'-p',str(r['port']),'-f',str(dial/1000),'-m','usb','-L','100','-H','3000','-r','12000','-d',str(d),'--station',rid,'--dt-sec','15','--connect-retries','1','--busy-retries','1','--log','warn'];started[rid]=time.time();procs[rid]=subprocess.Popen(cmd,stdout=subprocess.DEVNULL,stderr=(d/'recorder.stderr').open('w'),start_new_session=True)
 except Exception:
  stop_all(procs);raise
 return procs,started
def wait_ready(root,roster,seconds=20):
 end=time.monotonic()+seconds
 while time.monotonic()<end:
  ok=[r for r in roster if any(p.stat().st_size>=4096 for p in (root/r['candidate_id']).glob('*.wav'))]
  if len(ok)>=MIN_QUORUM:return ok
  time.sleep(.25)
 return []
def finish_captures(root,roster,procs,started,tx=None,deadline=None):
 stop_all(procs)
 time.sleep(1);out=[]
 for r in roster:
  wavs=sorted((root/r['candidate_id']).glob('*.wav'));row={**r,'status':'unavailable','decodes':[]}
  if wavs:
   try:
    w=select_kiwi_slot(wavs,tx['slot_epoch']) if tx else wavs[-1]
    sweep=sweep_module();info=sweep.wav_info(w);dur=info['duration_s'];capture_start=kiwi_capture_start(w);capture_end=capture_start+dur
    row.update(status='short' if dur<15 else 'captured',wav=str(w.relative_to(root.parent)),duration_s=dur,capture_start_epoch=capture_start,capture_end_epoch=capture_end,sha256=digest(w),wav_meta=info,capture_timing={'source':'kiwirecorder_filename_utc','capture_start_epoch':capture_start,'sample_rate':info['rate']},decodes=[])
    if tx:
     slot=w.with_name('transmitted-slot.wav');slot_meta=extract_tx_slot(w,slot,{'capture_start_epoch':capture_start,'sample_rate':info['rate']},tx['slot_epoch']);row['tx_slot_wav']=str(slot.relative_to(root.parent));row['tx_slot']=slot_meta;row['complete_tx_slot']=True;row['decodes']=decode_wav(slot,require_time(deadline,5) if deadline else 60)
     row['exact_matches']=[d for d in row['decodes'] if d['message']==tx['message'] and abs(d['offset_hz']-tx['audio_offset_hz'])<=10]
     if row['exact_matches']:row['status']='exact'
     else:row['status']='zero-decode'
    elif dur>=15:
     row['decodes']=decode_wav(w,require_time(deadline,5) if deadline else 60);row['status']='ambient-ft8' if row['decodes'] else 'zero-decode'
   except Exception as e:row.update(status='invalid-wav',error=repr(e))
  out.append(row)
 return out

def remote_beacon(runroot,label,band,offset,power,deadline,run_id):
 if process_collision():raise RuntimeError(f'RF collision immediately before {label} beacon')
 require_time(deadline,50);invoked=time.time();token=uuid.uuid4().hex
 remote=f'/tmp/ft8-propagation-{label}-{run_id}-{token}.json';local=runroot/f'{label}-tx-{token}.json';reason=f'hourly FT8 propagation {label} beacon {run_id}'
 q=sh(['ssh','-n','rpi-gateway',f"set -Eeuo pipefail;R={RADIO};cleanup(){{$R unkey >/dev/null 2>&1||true;$R tx-disable >/dev/null 2>&1||true;}};trap cleanup EXIT INT TERM HUP;$R tx-enable '{reason}' >/tmp/ft8-prop-enable-{token}.json;PYTHONPATH={REMOTE_PY} python3 {REMOTE_TX} --band {band} --offset {offset} --power {power} --message '{MESSAGE}' --run-id '{run_id}' --output {remote} --allow-tx"],require_time(deadline,20))
 atomic(runroot/f'{label}-tx-command.json',{'run_id':run_id,'remote_receipt':remote,'exit':q.returncode,'stdout':q.stdout,'stderr':q.stderr})
 if q.returncode:raise RuntimeError(f'{label} beacon SSH failed ({q.returncode}): {q.stderr[-300:]}')
 copy=sh(['scp','-q',f'rpi-gateway:{remote}',str(local)],require_time(deadline,5))
 if copy.returncode:raise RuntimeError(f'{label} beacon SCP failed ({copy.returncode}): {copy.stderr[-300:]}')
 try:doc=json.loads(local.read_text())
 except Exception as e:raise RuntimeError(f'{label} receipt unreadable: {e}')
 doc['exit']=q.returncode
 safe=doc.get('post_state',{}).get('ptt') is False and doc.get('post_state',{}).get('tx_enabled') is False
 fresh=doc.get('run_id')==run_id and float(doc.get('started_epoch',0))>=invoked and float(doc.get('finished_epoch',0))>=invoked
 if not (doc.get('sent') and fresh and safe and not doc.get('restore_error')):raise RuntimeError(f'{label} receipt rejected: stale, unsafe, unrestored, or unsuccessful')
 return doc

def tune_band(band,deadline=None):
 if process_collision():raise RuntimeError('RF collision immediately before tune')
 q=sh(['ssh','-n','rpi-gateway',RADIO,'freq-tune',str(FT8_DIAL[band])],require_time(deadline,10) if deadline else 100)
 try:d=json.loads(q.stdout)
 except Exception:raise RuntimeError('tuner returned non-JSON')
 if q.returncode or not d.get('tune',{}).get('tuned'):raise RuntimeError(f'antenna not matched on {band}: {d}')
 return d

def choose_tunable_band(rows,deadline=None):
 failures=[]
 for row in ranked_bands(rows):
  try:return row,tune_band(row['band'],deadline)
  except RuntimeError as e:failures.append({'band':row['band'],'error':str(e)})
 raise RuntimeError(f'no ranked band safely matched: {failures}')

def seal(root,decision):
 atomic(root/'admission.json',decision);files=sorted(p for p in root.rglob('*') if p.is_file() and p.name not in {'SHA256SUMS','SHA256SUMS.json'});atomic(root/'SHA256SUMS.json',{'files':[{'path':str(p.relative_to(root)),'sha256':digest(p)} for p in files]});files.append(root/'SHA256SUMS.json');atomic_text(root/'SHA256SUMS',''.join(f'{digest(p)}  {p.relative_to(root)}\n' for p in files));q=sh(['sha256sum','-c','SHA256SUMS'],60,root);return q.returncode==0

def main():
 p=argparse.ArgumentParser();p.add_argument('--deadline-epoch',type=float,required=True);p.add_argument('--no-rf',action='store_true');p.add_argument('--window-s',type=int,default=900);a=p.parse_args()
 RUNS.mkdir(parents=True,exist_ok=True);LOCK.parent.mkdir(parents=True,exist_ok=True);lockfh=LOCK.open('a+');active={};state=None;row=None;station_orig=None;root=None
 try:
  # The lock protects the state read, no-replay check, reservation, and all RF work.
  fcntl.flock(lockfh,fcntl.LOCK_EX|fcntl.LOCK_NB)
  now=time.time();require_time(a.deadline_epoch);key=hour_key(now);run_id=f'{key}-{uuid.uuid4().hex}'
  state=json.loads(STATE.read_text()) if STATE.exists() else {'schema':'ft8-propagation-state-v1','hours':[]}
  if not may_start_hour(state,key):return 0
  row={'key':key,'run_id':run_id,'reserved_epoch':now,'deadline_epoch':min(a.deadline_epoch,now+a.window_s),'status':'reserved','rf_authorized':not a.no_rf,'stages':[]};state['hours'].append(row);atomic(STATE,state)
  root=RUNS/run_id;root.mkdir(parents=True,exist_ok=False)
  collisions=process_collision()
  if collisions:row.update(status='blocked-concurrent-owner',collisions=collisions,finished_epoch=time.time());atomic(STATE,state);return 0
  station_orig=station_snapshot()
  require_time(row['deadline_epoch'],120);sweepdir=root/'sweep';q=sh([sys.executable,str(SWEEP),'--root',str(sweepdir),'--receivers','6'],require_time(row['deadline_epoch'],90,cap=420),HERE);row['stages'].append({'stage':'sweep','exit':q.returncode})
  if q.returncode:raise RuntimeError(f'sweep failed: {q.stderr[-500:]}')
  require_time(row['deadline_epoch']);sweep=json.loads((sweepdir/'sweep.json').read_text())
  attempts=[];pick=offset=remote_clear=local_clear=None;tuner_receipt=None
  for candidate in ranked_bands(sweep['bands']):
   attempt={'band':candidate['band']}
   try:
    occupied=[d['offset_hz'] for r in candidate['rows'] for d in r.get('decodes',[])]
    candidate_offset,candidate_remote=choose_clear_offset(candidate['rows'],occupied)
    candidate_local=local_clearance_capture(root/f"pre-tx-local-{candidate['band']}",candidate['dial_hz'],candidate_offset)
    attempt.update(offset_hz=candidate_offset,remote_clearance_count=len(candidate_remote),local_clear=candidate_local.get('clear',False))
    if not pre_tx_clearance(candidate_remote,candidate_local):raise RuntimeError('remote/local spectral clearance failed')
    candidate_tuner=None if a.no_rf else tune_band(candidate['band'],row['deadline_epoch'])
    pick,offset,remote_clear,local_clear,tuner_receipt=candidate,candidate_offset,candidate_remote,candidate_local,candidate_tuner
    attempt['accepted']=True;attempts.append(attempt);break
   except Exception as e:
    attempt.update(accepted=False,error=repr(e));attempts.append(attempt)
  atomic(root/'band-selection-attempts.json',attempts)
  if pick is None:raise RuntimeError(f'no ranked band passed clearance/match gates: {attempts}')
  row.update(selected_band=pick['band'],selected_dial_hz=pick['dial_hz'],audio_offset_hz=offset,remote_clearance_count=len(remote_clear),tuner=tuner_receipt)
  atomic(root/'pre-tx-clearance.json',{'offset_hz':offset,'remote':remote_clear,'local':local_clear,'attempts':attempts})
  if a.no_rf:
   decision={'schema':'ft8-propagation-admission-v1','status':'no-rf-rehearsal','selected_band':pick,'audio_offset_hz':offset,'pre_tx_clearance':{'remote_count':len(remote_clear),'local':local_clear},'rf_performed':False};ok=seal(root,decision);row.update(status='no-rf-rehearsal-complete' if ok else 'seal-failed',ledger_ok=ok,finished_epoch=time.time());atomic(STATE,state);return 0 if ok else 2
  require_time(row['deadline_epoch'],420);fallback=[dict(r) for r in remote_clear][:6]
  valroot=root/'validation-captures';procs,started=start_captures(valroot,fallback,pick['dial_hz']);active.update(procs)
  require_time(row['deadline_epoch'],70);ready=wait_ready(valroot,fallback,min(20,require_time(row['deadline_epoch'],50)))
  if len(ready)<MIN_QUORUM:raise RuntimeError('validation SDR quorum failed')
  if process_collision():raise RuntimeError('RF collision immediately before validation beacon')
  validation=remote_beacon(root,'validation',pick['band'],offset,10,row['deadline_epoch'],run_id);require_time(row['deadline_epoch'],18);time.sleep(18)
  validation_rows=finish_captures(valroot,fallback,active,started,validation,row['deadline_epoch']);atomic(root/'validation-captures.json',validation_rows);row['stages'].append({'stage':'validation-beacon','slot_epoch':validation['slot_epoch']})
  # Query once after PSKReporter's documented reporting interval. The reporter-
  # guided roster is for the next hourly cycle; the current beacon is validated
  # independently from the already-recording fallback SDRs.
  wait_until=validation['slot_epoch']+300;delay=max(0,wait_until-time.time());require_time(row['deadline_epoch'],delay+90);time.sleep(delay)
  params=urllib.parse.urlencode({'senderCallsign':CALL,'flowStartSeconds':-900,'rronly':1});raw=fetch(PSK+'?'+params,min(45,require_time(row['deadline_epoch'],45)));(root/'pskreporter.xml').write_bytes(raw);all_reports=parse_psk(raw);reports=filter_reports(all_reports,pick['band'],validation['slot_epoch']);atomic(root/'pskreporter-filtered.json',reports)
  html=fetch(RECEIVERBOOK,min(45,require_time(row['deadline_epoch'],30))).decode('utf-8');(root/'receiverbook.html').write_text(html);loc=locator_module();inventory=loc.parse_receiverbook(html);normalized=[]
  for rr in reports:
   ll=loc.grid_to_ll(rr['grid'])
   if ll:normalized.append({**rr,'lat':ll[0],'lon':ll[1]})
  ranked=loc.rank(normalized,inventory,500,5);guided=unique_sdrs(ranked,8);next_roster=merge_unique_sdrs(guided,fallback,8)
  atomic(root/'next-hour-roster.json',{'schema':'ft8-reporter-guided-roster-v1','created_epoch':time.time(),'valid_until_epoch':time.time()+3600,'band':pick['band'],'reports':reports,'receivers':next_roster})
  ambient=[x for x in validation_rows if x.get('decodes')];admission=admission_result(validation_rows,reports);status=admission['status']
  closeout=restore_station(station_orig);station_orig=None
  if not (closeout.get('safe') and closeout.get('restored')):raise RuntimeError(f'station closeout verification failed: {closeout}')
  decision={'schema':'ft8-propagation-admission-v1','hour':key,'run_id':run_id,**admission,'selected_band':pick,'audio_offset_hz':offset,'pre_tx_clearance':{'remote_count':len(remote_clear),'local':local_clear},'validation_tx':validation,'psk_query':{'url':PSK+'?'+params,'query_epoch':time.time(),'fresh_filtered_reports':reports},'next_hour_receiver_roster':next_roster,'validation_rows':validation_rows,'ambient_receiver_count':len(ambient),'station_closeout':closeout,'rf_performed':True,'created_epoch':time.time()};ok=seal(root,decision);row.update(status=status,ledger_ok=ok,exact_receiver_count=admission['exact_receiver_count'],exact_site_count=admission['exact_site_count'],psk_report_count=len(reports),finished_epoch=time.time());atomic(STATE,state);return 0 if ok else 2
 except BlockingIOError:return 0
 except Exception as e:
  if row is not None and state is not None:
   row.update(status='failed',error=repr(e),finished_epoch=time.time());atomic(STATE,state)
   if root is not None and root.exists():
    try:seal(root,{'schema':'ft8-propagation-attempt-v1','status':'failed','run_id':row.get('run_id'),'hour':row.get('key'),'error':repr(e),'rf_authorized':row.get('rf_authorized'),'rf_performed':any(root.glob('*-tx-*.json')),'created_epoch':time.time()})
    except Exception as seal_error:row['seal_error']=repr(seal_error);atomic(STATE,state)
  if not a.no_rf:sh(['ssh','-n','rpi-gateway',f'{RADIO} unkey >/dev/null 2>&1||true;{RADIO} tx-disable >/dev/null 2>&1||true'],30)
  return 2
 finally:
  stop_all(active)
  if station_orig is not None:
   try:
    closeout=restore_station(station_orig)
    if row is not None and state is not None:
     row['final_closeout']=closeout
     if not (closeout.get('safe') and closeout.get('restored')):row['status']='failed-closeout'
     atomic(STATE,state)
   except Exception as e:
    if row is not None and state is not None:row.update(status='failed-closeout',closeout_error=repr(e));atomic(STATE,state)
  try:fcntl.flock(lockfh,fcntl.LOCK_UN)
  except Exception:pass
  lockfh.close()
if __name__=='__main__':raise SystemExit(main())
