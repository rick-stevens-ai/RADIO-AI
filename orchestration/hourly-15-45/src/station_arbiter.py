"""Cross-process exclusive ownership for station CAT/RF operations."""
import fcntl,pathlib
class StationBusy(RuntimeError):pass
class StationArbiter:
 def __init__(self,path='/home/stevens/radio/rf-campaign.lock'):self.path=pathlib.Path(path);self.handle=None
 def acquire(self):
  self.path.parent.mkdir(parents=True,exist_ok=True);self.handle=self.path.open('a+')
  try:fcntl.flock(self.handle.fileno(),fcntl.LOCK_EX|fcntl.LOCK_NB)
  except BlockingIOError:
   self.handle.close();self.handle=None;raise StationBusy('station RF/CAT owner active')
  return self
 def release(self):
  if self.handle is not None:fcntl.flock(self.handle.fileno(),fcntl.LOCK_UN);self.handle.close();self.handle=None
 def __enter__(self):return self.acquire()
 def __exit__(self,*args):self.release()
