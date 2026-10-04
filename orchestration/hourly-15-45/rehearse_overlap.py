#!/usr/bin/env python3
"""Compressed no-RF rehearsal of the 15/45 two-lane hourly schedule."""
from __future__ import annotations
import argparse,json,pathlib,sys,threading,time
from concurrent.futures import ThreadPoolExecutor
sys.path.insert(0,str(pathlib.Path(__file__).parent/'src'))
from hourly_scheduler import EventRecorder,JobSpec,MODES,build_cycle

def run(scale:float,out:pathlib.Path):
 plan=build_cycle(0);rec=EventRecorder();guard=threading.Lock();events=[];epoch=time.monotonic()
 def emit(kind,**kw):
  with guard:events.append({'kind':kind,'epoch':time.monotonic(),**kw})
 def job(spec):
  delay=epoch+spec.start*scale-time.monotonic()
  if delay>0:time.sleep(delay)
  with rec.interval(spec):
   emit('start',name=spec.name,lane=spec.lane)
   time.sleep(max(.005,(spec.end-spec.start)*scale))
   emit('end',name=spec.name,lane=spec.lane)
 ft8_rf=[JobSpec(f'FT8-TX-{s.offset:03d}','rf',s.offset,s.offset+15) for s in plan.ft8_slots if s.direction=='TX']
 ft8_rx=[JobSpec(f'FT8-RX-{s.offset:03d}','processing',s.offset,s.offset+15) for s in plan.ft8_slots if s.direction=='RX']
 all_rf=sorted(ft8_rf+list(plan.rf_cells),key=lambda x:x.start)
 def rf_lane():
  for spec in all_rf:job(spec)
 with ThreadPoolExecutor(max_workers=len(plan.jobs)+len(ft8_rx)+1) as pool:
  futures=[pool.submit(job,j) for j in plan.jobs+tuple(ft8_rx)]
  futures.append(pool.submit(rf_lane))
  for f in futures:f.result()
 intervals=[vars(x) for x in rec.intervals()]
 def overlap(a,b):return a['start']<b['end'] and b['start']<a['end']
 rf=[x for x in intervals if x['lane']=='rf'];proc=[x for x in intervals if x['lane']=='processing']
 ordered=sorted(rf,key=lambda x:x['start']);rf_exclusive=all(a['end']<=b['start'] for a,b in zip(ordered,ordered[1:]))
 doc={'schema':'hourly-15-45-overlap-rehearsal-v2','rf_performed':False,'ft8_tx_count':sum(x['name'].startswith('FT8-TX') for x in rf),'ft8_rx_count':sum(x['name'].startswith('FT8-RX') for x in proc),'rf_modes':[x['name'] for x in rf if x['name'] in MODES],'offline_modes':[x['name'] for x in proc if x['name']=='BPSK'],'processing_jobs':len(proc),'rf_processing_overlap_pairs':sum(overlap(a,b) for a in rf for b in proc),'every_rf_interval_overlaps_processing':all(any(overlap(a,b) for b in proc) for a in rf),'rf_lane_exclusive':rf_exclusive,'intervals':intervals,'events':events}
 out.parent.mkdir(parents=True,exist_ok=True);out.write_text(json.dumps(doc,indent=2)+'\n');print(json.dumps({k:v for k,v in doc.items() if k not in ('intervals','events')},indent=2));return 0 if doc['ft8_tx_count']==30 and doc['ft8_rx_count']==30 and len(doc['rf_modes'])==7 and doc['offline_modes']==['BPSK'] and doc['every_rf_interval_overlaps_processing'] and doc['rf_lane_exclusive'] else 2
if __name__=='__main__':
 p=argparse.ArgumentParser();p.add_argument('--scale',type=float,default=.0002);p.add_argument('--output',type=pathlib.Path,required=True);a=p.parse_args();raise SystemExit(run(a.scale,a.output))
