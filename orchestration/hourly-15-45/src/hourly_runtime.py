"""No-RF composition runtime for the hourly 15/45 pipeline."""
from __future__ import annotations
from dataclasses import asdict,dataclass
import json,pathlib,time
from hourly_scheduler import MODES,CycleStateStore,ScheduleError,build_cycle,cycle_window,slot_admission

@dataclass(frozen=True)
class RuntimeEvent:
 kind:str;name:str;offset:float;status:str;reason:str=''
@dataclass(frozen=True)
class RuntimeReport:
 anchor_epoch:float;deadline_epoch:float;events:tuple[RuntimeEvent,...];rf_performed:bool=False
 def to_json(self):return json.dumps(asdict(self),indent=2)

class NoRFRuntime:
 """Builds immutable reservations and invokes only injected no-RF adapters."""
 def __init__(self,state_path,clock,ft8_dry_runner,assignment_builder,mode_dry_runners,processing_lane=None,scout_jobs=None,mode_processors=None,station_arbiter=None,persistent_scout=None):
  self.state=CycleStateStore(state_path);self.clock=clock;self.ft8=ft8_dry_runner;self.assignment_builder=assignment_builder;self.modes=mode_dry_runners;self.processing=processing_lane;self.scout_jobs=scout_jobs or {};self.mode_processors=mode_processors or {};self.station_arbiter=station_arbiter;self.persistent_scout=persistent_scout
 def run(self):
  if self.station_arbiter:
   with self.station_arbiter:return self._run_owned()
  return self._run_owned()
 def _run_owned(self):
  window=cycle_window(self.clock.time());plan=build_cycle(int(window.anchor_epoch//3600));events=[]
  if self.processing:
   for name,fn in self.scout_jobs.items():self.processing.submit(name,fn)
   self.processing.submit('assignment',self.assignment_builder,window)
  # Persistent scout is authoritative when configured; it owns all 60 FT8 slots.
  if self.persistent_scout:
   report=self.persistent_scout.run()
   if report.tx_completed+report.rx_completed+report.skipped != 60:raise RuntimeError('incomplete persistent scout accounting')
   events.append(RuntimeEvent('scout','FT8',0,'completed',f'tx={report.tx_completed},rx={report.rx_completed},skipped={report.skipped}'))
  else:
   # Isolated fallback used only by no-RF unit tests.
   for slot in plan.ft8_slots:
    key=f'FT8-{slot.direction}-{slot.offset:03d}'
    admission=slot_admission(window.anchor_epoch,slot.offset,self.clock.time(),1.0)
    if admission.action=='wait':self.clock.sleep_until(admission.slot_epoch);admission=slot_admission(window.anchor_epoch,slot.offset,self.clock.time(),1.0)
    if admission.action=='skip':events.append(RuntimeEvent('slot',key,slot.offset,'skipped',admission.reason));continue
    try:self.state.reserve(str(int(window.anchor_epoch)),key)
    except ScheduleError:events.append(RuntimeEvent('slot',key,slot.offset,'skipped','reserved-no-replay'));continue
    if slot.direction=='TX':
     result=self.ft8(slot,window)
     if result.get('rf_performed'):raise RuntimeError('no-RF adapter performed RF')
    events.append(RuntimeEvent('slot',key,slot.offset,'completed'))
  assignment=self.processing.require('assignment') if self.processing else self.assignment_builder(window)
  # Offline BPSK is exercised on processing lane, never RF.
  for cell in plan.offline_cells:
   try:self.state.reserve(str(int(window.anchor_epoch)),cell.name)
   except ScheduleError:events.append(RuntimeEvent('mode',cell.name,cell.start,'skipped','reserved-no-replay'));continue
   result=self.modes[cell.name](assignment,cell,window)
   if result.get('rf_performed'):raise RuntimeError('offline adapter performed RF')
   if self.processing:self.processing.submit(f'process-{cell.name}',lambda value=result:value)
   events.append(RuntimeEvent('offline-mode',cell.name,cell.start,'completed'))
  # Assignment is the only mandatory minute-15 processing join.
  for cell in plan.rf_cells:
   admission=slot_admission(window.anchor_epoch,cell.start,self.clock.time(),1.0)
   if admission.action=='wait':self.clock.sleep_until(admission.slot_epoch);admission=slot_admission(window.anchor_epoch,cell.start,self.clock.time(),1.0)
   if admission.action=='skip':events.append(RuntimeEvent('mode',cell.name,cell.start,'skipped',admission.reason));continue
   try:self.state.reserve(str(int(window.anchor_epoch)),cell.name)
   except ScheduleError:events.append(RuntimeEvent('mode',cell.name,cell.start,'skipped','reserved-no-replay'));continue
   result=self.modes[cell.name](assignment,cell,window)
   if result.get('rf_performed'):raise RuntimeError('no-RF adapter performed RF')
   if self.processing and cell.name in self.mode_processors:self.processing.submit(f'process-{cell.name}',self.mode_processors[cell.name],result)
   events.append(RuntimeEvent('mode',cell.name,cell.start,'completed'))
  if self.processing:self.processing.close_at_deadline(window.deadline_epoch)
  return RuntimeReport(window.anchor_epoch,window.deadline_epoch,tuple(events),False)
