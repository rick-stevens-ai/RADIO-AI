import pathlib,sys
import pytest
sys.path.insert(0,str(pathlib.Path(__file__).resolve().parents[1]/'src'))
from hourly_runtime import NoRFRuntime
from hourly_scheduler import MODES

class Clock:
 def __init__(self,t):self.t=t
 def time(self):return self.t
 def sleep_until(self,t):self.t=max(self.t,t)
def runtime(tmp_path,start=1_800_000_000.0,ft8=None,modes=None,builder=None):
 clock=Clock(start);calls={'ft8':[],'modes':[],'assign':0}
 def f(slot,window):calls['ft8'].append(slot.offset);return {'rf_performed':False}
 def b(window):calls['assign']+=1;return {'sealed':True}
 def m(name):return lambda assignment,cell,window:(calls['modes'].append(name) or {'rf_performed':False})
 runners={name:m(name) for name in MODES};r=NoRFRuntime(tmp_path/'state.json',clock,ft8 or f,builder or b,modes or runners);return r,clock,calls

def test_full_no_rf_hour_reserves_30_tx_30_rx_and_all_modes(tmp_path):
 r,c,calls=runtime(tmp_path);report=r.run()
 assert report.rf_performed is False
 assert len([e for e in report.events if e.kind=='slot'])==60
 assert len(calls['ft8'])==30
 assert calls['modes']==['BPSK']+[mode for mode in MODES if mode!='BPSK']
 assert calls['assign']==1
 assert c.time()<=report.deadline_epoch

def test_restart_never_replays_reserved_slots_or_modes(tmp_path):
 r,c,calls=runtime(tmp_path);r.run();r2,c2,calls2=runtime(tmp_path);report=r2.run()
 assert calls2['ft8']==[] and calls2['modes']==[]
 assert all(e.status=='skipped' for e in report.events)

def test_late_start_skips_elapsed_slots_without_catchup(tmp_path):
 r,c,calls=runtime(tmp_path,start=1_800_000_061.1);report=r.run()
 skipped=[e for e in report.events if e.status=='skipped']
 assert len(skipped)>=5
 assert 0 not in calls['ft8'] and 2 not in calls['ft8'] and 4 not in calls['ft8']

def test_assignment_failure_prevents_all_mode_dispatch(tmp_path):
 def broken(window):raise RuntimeError('no assignment')
 r,c,calls=runtime(tmp_path,builder=broken)
 with pytest.raises(RuntimeError,match='no assignment'):r.run()
 assert calls['modes']==[]

def test_any_adapter_claiming_rf_is_rejected(tmp_path):
 def bad(slot,window):return {'rf_performed':True}
 r,c,calls=runtime(tmp_path,ft8=bad)
 with pytest.raises(RuntimeError,match='performed RF'):r.run()

def test_runtime_composes_one_persistent_scout_session(tmp_path):
 from types import SimpleNamespace
 r,c,calls=runtime(tmp_path)
 class Scout:
  def __init__(self):self.calls=0
  def run(self):self.calls+=1;c.sleep_until(1_800_000_900);return SimpleNamespace(tx_completed=30,rx_completed=30,skipped=0)
 scout=Scout();r.persistent_scout=scout;report=r.run()
 assert scout.calls==1 and calls['ft8']==[]
 assert next(e for e in report.events if e.kind=='scout').reason=='tx=30,rx=30,skipped=0'

def test_runtime_holds_station_arbiter_for_entire_cycle(tmp_path):
 class Arbiter:
  def __init__(self):self.entered=0;self.exited=0
  def __enter__(self):self.entered+=1;return self
  def __exit__(self,*a):self.exited+=1
 r,c,calls=runtime(tmp_path);arb=Arbiter();r.station_arbiter=arb;r.run()
 assert arb.entered==1 and arb.exited==1

def test_mode_adapter_claiming_rf_is_rejected(tmp_path):
 r,c,calls=runtime(tmp_path)
 r.modes[MODES[0]]=lambda *a:{'rf_performed':True}
 with pytest.raises(RuntimeError,match='performed RF'):r.run()

class FakeProcessingLane:
 def __init__(self):self.submitted=[];self.values={};self.required=[];self.closed=False
 def submit(self,name,fn,*args):
  self.submitted.append(name)
  if name=='assignment':self.values[name]=fn(*args)
  else:self.values[name]=('pending',fn,args)
 def require(self,name,timeout=None):self.required.append(name);return self.values[name]
 def close_at_deadline(self,deadline):self.closed=True;return []

def test_runtime_wires_processing_lane_and_only_joins_assignment(tmp_path):
 r,c,calls=runtime(tmp_path);lane=FakeProcessingLane();seen=[]
 r.processing=lane;r.scout_jobs={'band-sweep':lambda:1,'pskreporter':lambda:2}
 r.mode_processors={name:(lambda result,n=name:seen.append(n)) for name in MODES}
 report=r.run()
 assert lane.submitted[:3]==['band-sweep','pskreporter','assignment']
 assert lane.required==['assignment']
 expected=['BPSK']+[name for name in MODES if name!='BPSK']
 assert lane.submitted[3:]==[f'process-{name}' for name in expected]
 assert lane.closed is True and report.rf_performed is False
 assert seen==[]  # optional mode processors were submitted, not synchronously joined
