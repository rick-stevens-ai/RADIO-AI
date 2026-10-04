"""Persistent receiver-session manager owned by the processing lane."""
from __future__ import annotations
from dataclasses import dataclass
import pathlib
@dataclass
class ReceiverSession:
 receiver_id:str;frequency_hz:int;process:object;generation:int
class ReceiverSessionManager:
 def __init__(self,lane,command_builder,root):self.lane=lane;self.command_builder=command_builder;self.root=pathlib.Path(root);self.sessions={};self.generation=0
 def ensure(self,receiver,frequency_hz):
  rid=receiver['candidate_id'];current=self.sessions.get(rid)
  if current and current.frequency_hz==frequency_hz and current.process.poll() is None:return current
  if current and current.process.poll() is None:
   self.lane.stop_recorder(rid)
  self.generation+=1;directory=self.root/f'g{self.generation:03d}'/rid;argv=self.command_builder(receiver,frequency_hz,directory);proc=self.lane.start_recorder(rid,argv,directory/'stderr.txt');session=ReceiverSession(rid,frequency_hz,proc,self.generation);self.sessions[rid]=session;return session
 def prepare_roster(self,roster,frequency_hz):return [self.ensure(row,frequency_hz) for row in roster]
