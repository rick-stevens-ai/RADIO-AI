import multiprocessing,pathlib,sys
import pytest
sys.path.insert(0,str(pathlib.Path(__file__).resolve().parents[1]/'src'))
from station_arbiter import StationArbiter,StationBusy

def child(path,queue):
 try:
  with StationArbiter(path):queue.put('owned')
 except StationBusy:queue.put('busy')
def test_cross_process_second_owner_is_rejected(tmp_path):
 path=tmp_path/'rf.lock';ctx=multiprocessing.get_context('fork');q=ctx.Queue()
 with StationArbiter(path):
  p=ctx.Process(target=child,args=(path,q));p.start();p.join(2);assert q.get(timeout=1)=='busy'
 p=ctx.Process(target=child,args=(path,q));p.start();p.join(2);assert q.get(timeout=1)=='owned'
def test_context_releases_after_exception(tmp_path):
 path=tmp_path/'rf.lock'
 with pytest.raises(RuntimeError):
  with StationArbiter(path):raise RuntimeError('x')
 with StationArbiter(path):pass
