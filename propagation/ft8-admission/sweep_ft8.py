#!/usr/bin/env python3
"""Parallel public-SDR FT8 sweep over the nine routine bands."""
import argparse,concurrent.futures,hashlib,json,math,os,pathlib,signal,struct,subprocess,tempfile,time,wave
import numpy as np
from datetime import datetime,timezone
from propagation_admission import FT8_DIAL
KIWI='/home/stevens/sdr/kiwiclient/kiwirecorder.py'
DEFAULT_ROSTER='/home/stevens/sdr/ota-wide-40m-probe-20260923/receiver-roster.json'
FT8_OFFSETS=(600,900,1200,1500,1800,2100,2400)
LANE_CLEAR_DB=15.0

def wav_info(path):
 with wave.open(str(path),'rb') as w:
  channels,width,rate,frames=w.getnchannels(),w.getsampwidth(),w.getframerate(),w.getnframes()
 if channels!=1 or width!=2 or rate!=12000:raise ValueError(f'not mono PCM16 12 kHz: {path}')
 return {'channels':channels,'width':width,'rate':rate,'frames':frames,'duration_s':frames/rate}

def lane_metric(path,offset_hz,lane_half_width=60,guard_hz=60):
 """Median 1 s Hann/50%-overlap lane peak relative to nearby background."""
 info=wav_info(path)
 with wave.open(str(path),'rb') as w:x=np.frombuffer(w.readframes(w.getnframes()),dtype='<i2').astype(float)
 n=info['rate'];hop=n//2;window=np.hanning(n);spectra=[]
 for start in range(0,len(x)-n+1,hop):spectra.append(np.abs(np.fft.rfft(x[start:start+n]*window))**2)
 if not spectra:raise ValueError('capture shorter than one complete second')
 med=np.median(np.stack(spectra),axis=0);hz=np.fft.rfftfreq(n,1/n)
 lane=med[(hz>=offset_hz-lane_half_width)&(hz<=offset_hz+lane_half_width)]
 adjacent=med[((hz>=offset_hz-3*lane_half_width-guard_hz)&(hz<offset_hz-lane_half_width-guard_hz))|((hz>offset_hz+lane_half_width+guard_hz)&(hz<=offset_hz+3*lane_half_width+guard_hz))]
 if not len(lane) or not len(adjacent):raise ValueError('selected lane outside measurable passband')
 return float(10*np.log10(max(float(lane.max()),1e-18)/max(float(np.median(adjacent)),1e-18)))

def stop(p):
 try:os.killpg(p.pid,signal.SIGINT);p.wait(timeout=5)
 except Exception:
  try:os.killpg(p.pid,signal.SIGKILL)
  except Exception:pass

def decode(wav):
 wav=pathlib.Path(wav).resolve()
 with tempfile.TemporaryDirectory(prefix='jt9-isolated-') as td:
  q=subprocess.run(['/usr/bin/jt9','-8','-d','3','-a',td,'-t',td,str(wav)],capture_output=True,text=True,timeout=45,cwd=td)
 out=[]
 for line in q.stdout.splitlines():
  z=line.split(maxsplit=5)
  if len(z)>=6 and z[0].isdigit():
   try:out.append({'utc':z[0],'snr_db':int(z[1]),'dt_s':float(z[2]),'offset_hz':int(z[3]),'message':z[5].strip()})
   except ValueError:pass
 return out

def capture_band(root,band,dial,roster):
 d=root/band;d.mkdir(parents=True,exist_ok=True);procs={}
 # Start just before one common UTC boundary; record enough for one complete slot.
 now=time.time();boundary=(int(now//15)+1)*15;time.sleep(max(0,boundary-now-1.5))
 for r in roster:
  q=d/r['candidate_id'];q.mkdir();base=q/'capture';cmd=['timeout','--kill-after=3','--signal=INT','22','python3',KIWI,'-s',r['host'],'-p',str(r['port']),'-f',str(dial/1000),'-m','usb','-L','100','-H','3000','-r','12000','--fn',str(base),'--connect-retries','1','--busy-retries','1','--log','warn'];procs[r['candidate_id']]=subprocess.Popen(cmd,stdout=subprocess.DEVNULL,stderr=(q/'stderr.txt').open('w'),start_new_session=True)
 time.sleep(19)
 for p in procs.values():stop(p)
 rows=[]
 for r in roster:
  wavs=sorted((d/r['candidate_id']).glob('capture*.wav'));w=wavs[-1] if wavs else None;row={**r,'receiver_id':r['candidate_id'],'host':r['host'],'port':r['port'],'available':False,'clear':False,'decodes':[]}
  if w:
   try:
    info=wav_info(w);dur=info['duration_s'];metrics={str(o):lane_metric(w,o) for o in FT8_OFFSETS}
    row.update(available=dur>=13,clear=any(v<LANE_CLEAR_DB for v in metrics.values()),duration_s=dur,rate=info['rate'],wav_meta=info,lane_metrics_db=metrics,path=str(w.relative_to(root)),sha256=hashlib.sha256(w.read_bytes()).hexdigest(),decodes=decode(w) if dur>=13 else [])
   except Exception as e:row['error']=repr(e)
  rows.append(row)
 calls={}
 for x in rows:
  for z in x['decodes']:
   parts=z['message'].split();call=parts[1] if len(parts)>1 and parts[0]=='CQ' else (parts[1] if len(parts)>1 else None)
   if call:calls[call]=max(calls.get(call,-99),z['snr_db'])
 return {'band':band,'dial_hz':dial,'slot_epoch':boundary,'available_receivers':sum(x['available'] for x in rows),'clear_receivers':sum(x.get('clear',False) for x in rows),'lane_clear_threshold_db':LANE_CLEAR_DB,'n_decodes':sum(len(x['decodes']) for x in rows),'unique_calls':len(calls),'best_snr_db':max(calls.values(),default=-99),'rows':rows}

def main():
 p=argparse.ArgumentParser();p.add_argument('--root',type=pathlib.Path,required=True);p.add_argument('--roster',default=DEFAULT_ROSTER);p.add_argument('--receivers',type=int,default=6);a=p.parse_args();a.root.mkdir(parents=True,exist_ok=False);roster=json.load(open(a.roster))['receivers'][:a.receivers];started=time.time();bands=[]
 for band,dial in FT8_DIAL.items():bands.append(capture_band(a.root,band,dial,roster))
 doc={'schema':'ft8-nine-band-sweep-v1','created_utc':datetime.now(timezone.utc).isoformat(),'started_epoch':started,'finished_epoch':time.time(),'elapsed_s':time.time()-started,'rf_performed':False,'bands':bands};(a.root/'sweep.json').write_text(json.dumps(doc,indent=2)+'\n');print(json.dumps(doc));return 0
if __name__=='__main__':raise SystemExit(main())
