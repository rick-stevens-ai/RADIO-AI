"""Persistent 15-minute FT8 scout controller with injected station adapters."""
from __future__ import annotations
from dataclasses import asdict,dataclass
import json,os,pathlib

class ScoutFault(RuntimeError):pass
@dataclass(frozen=True)
class ScoutConfig:
 anchor_epoch:float;max_late_s:float=.5
 @property
 def deadline_epoch(self):return self.anchor_epoch+900
@dataclass(frozen=True)
class SlotEvent:
 slot_index:int;direction:str;planned_epoch:float
@dataclass(frozen=True)
class ScoutReport:
 anchor_epoch:float;deadline_epoch:float;tx_completed:int;rx_completed:int;skipped:int;callback_submissions:int
class SlotStateStore:
 def __init__(self,path):self.path=pathlib.Path(path)
 def load(self):return json.loads(self.path.read_text()) if self.path.exists() else {'schema':'ft8-scout-state-v1','started':False,'slots':{}}
 def write(self,d):
  self.path.parent.mkdir(parents=True,exist_ok=True);tmp=self.path.with_suffix('.tmp');
  with tmp.open('w') as f:json.dump(d,f,indent=2,sort_keys=True);f.write('\n');f.flush();os.fsync(f.fileno())
  os.replace(tmp,self.path)
class ScoutController:
 def __init__(self,config,clock,rig,transmitter,state,rx_callbacks=(),submit_processing=None):self.config=config;self.clock=clock;self.rig=rig;self.transmitter=transmitter;self.state=state;self.rx_callbacks=tuple(rx_callbacks);self.submit_processing=submit_processing
 def _healthy(self):
  row=self.rig.telemetry();return bool(row.get('complete') and row.get('fresh') and not row.get('ptt') and not row.get('tx_enabled'))
 def run(self):
  doc=self.state.load()
  if doc.get('started'):raise ScoutFault('scout already started; no replay')
  doc.update(started=True,anchor_epoch=self.config.anchor_epoch,deadline_epoch=self.config.deadline_epoch);self.state.write(doc)
  original=self.rig.snapshot();tx=rx=skipped=submissions=0
  try:
   for index in range(60):
    planned=self.config.anchor_epoch+index*15;direction='TX' if index%2==0 else 'RX';now=self.clock.time()
    if now>=self.config.deadline_epoch:break
    if now>planned+self.config.max_late_s:
     doc['slots'][str(index)]={'direction':direction,'status':'skipped-late','planned_epoch':planned};skipped+=1;self.state.write(doc);continue
    self.clock.sleep_until(planned)
    if self.clock.time()>=self.config.deadline_epoch:break
    if direction=='RX':
     event=SlotEvent(index,direction,planned);doc['slots'][str(index)]={'direction':direction,'status':'completed','planned_epoch':planned};rx+=1
     for callback in self.rx_callbacks:
      if self.submit_processing is None:callback(event)
      else:self.submit_processing(f'ft8-rx-{index:02d}-{submissions:03d}',callback,event)
      submissions+=1
    else:
     if planned+15>self.config.deadline_epoch:doc['slots'][str(index)]={'direction':direction,'status':'skipped-deadline','planned_epoch':planned};skipped+=1;self.state.write(doc);continue
     if not self._healthy():raise ScoutFault(f'telemetry unsafe before slot {index}')
     doc['slots'][str(index)]={'direction':direction,'status':'reserved-no-replay','planned_epoch':planned};self.state.write(doc)
     try:receipt=self.transmitter.invoke(index,planned,self.config.deadline_epoch)
     except Exception as exc:
      doc['slots'][str(index)].update(status='failed',error=repr(exc));self.state.write(doc);raise ScoutFault(f'slot {index} transmitter fault') from exc
     if not receipt.get('sent') or not receipt.get('validated') or receipt.get('exit_code')!=0:raise ScoutFault(f'slot {index} invalid receipt')
     if not self._healthy():raise ScoutFault(f'telemetry unsafe after slot {index}')
     doc['slots'][str(index)].update(status='completed',receipt=receipt);tx+=1
    self.state.write(doc)
   self.clock.sleep_until(self.config.deadline_epoch)
   return ScoutReport(self.config.anchor_epoch,self.config.deadline_epoch,tx,rx,skipped,submissions)
  finally:
   self.rig.restore(original)
def build_dry_run(now_epoch,rig_factory=None):
 anchor=float(int(now_epoch//900)*900);return {'schema':'ft8-scout-dry-run-v1','dry_run':True,'rf_authorized':False,'anchor_epoch':anchor,'deadline_epoch':anchor+900,'slots':[{'slot_index':i,'direction':'TX' if i%2==0 else 'RX','planned_epoch':anchor+i*15,'action':'would-run'} for i in range(60)]}
