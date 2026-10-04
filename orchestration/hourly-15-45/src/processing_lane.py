#!/usr/bin/env python3
"""Persistent non-RF processing lane for an hourly radio cycle.

Owns receiver processes and asynchronous analysis tasks. It never controls CAT,
PTT, key lines, power, or RF authorization.
"""
from __future__ import annotations
from concurrent.futures import Future,ThreadPoolExecutor
from dataclasses import dataclass
import json,os,pathlib,signal,subprocess,threading,time

@dataclass(frozen=True)
class TaskReceipt:
 name:str; submitted_epoch:float; finished_epoch:float|None; status:str

class ProcessingLane:
 def __init__(self,max_workers:int=12):
  self.pool=ThreadPoolExecutor(max_workers=max_workers,thread_name_prefix='radio-processing')
  self.futures:dict[str,Future]={};self.submitted:dict[str,float]={};self.finished:dict[str,float]={};self.deadline_incomplete:set[str]=set();self.recorders:dict[str,subprocess.Popen]={};self.lock=threading.Lock()
 def submit(self,name,fn,*args,**kwargs):
  with self.lock:
   if name in self.futures:raise ValueError(f'duplicate processing task: {name}')
   self.submitted[name]=time.time()
   def wrapped():
    try:return fn(*args,**kwargs)
    finally:
     with self.lock:self.finished[name]=time.time()
   self.futures[name]=self.pool.submit(wrapped);return self.futures[name]
 def require(self,name,timeout=None):
  """Wait only when caller declares this task a mandatory gate."""
  return self.futures[name].result(timeout=timeout)
 def start_recorder(self,receiver_id,argv,stderr_path):
  with self.lock:
   if receiver_id in self.recorders and self.recorders[receiver_id].poll() is None:return self.recorders[receiver_id]
   path=pathlib.Path(stderr_path);path.parent.mkdir(parents=True,exist_ok=True);handle=path.open('a');proc=subprocess.Popen(argv,stdout=subprocess.DEVNULL,stderr=handle,start_new_session=True);self.recorders[receiver_id]=proc;return proc
 def stop_recorder(self,receiver_id):
  proc=self.recorders.get(receiver_id)
  if proc is None or proc.poll() is not None:return
  try:os.killpg(proc.pid,signal.SIGINT);proc.wait(timeout=8)
  except Exception:
   try:os.killpg(proc.pid,signal.SIGKILL);proc.wait(timeout=3)
   except Exception:pass
 def stop_recorders(self):
  for receiver_id in list(self.recorders):self.stop_recorder(receiver_id)
 def receipts(self):
  rows=[]
  for name,f in self.futures.items():
   if name in self.deadline_incomplete:status='incomplete-at-deadline'
   else:status='running' if not f.done() else ('failed' if f.exception() else 'finished')
   rows.append(TaskReceipt(name,self.submitted[name],self.finished.get(name),status).__dict__)
  return rows
 def close_at_deadline(self,deadline_epoch):
  """Never wait past deadline; classify running work and cancel queued work."""
  for name,f in self.futures.items():
   if not f.done():self.deadline_incomplete.add(name);f.cancel()
  self.stop_recorders();self.pool.shutdown(wait=False,cancel_futures=True);return self.receipts()

if __name__=='__main__':
 print(json.dumps({'component':'processing-lane','rf_capable':False,'rf_performed':False}))
