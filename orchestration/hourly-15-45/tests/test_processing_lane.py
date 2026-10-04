import pathlib,sys,threading,time
import pytest
sys.path.insert(0,str(pathlib.Path(__file__).resolve().parents[1]/'src'))
from processing_lane import ProcessingLane

class FakeProc:
 def __init__(self):self.pid=123;self._poll=None
 def poll(self):return self._poll

def test_optional_work_does_not_block_caller():
 lane=ProcessingLane(2);release=threading.Event();started=threading.Event()
 def slow():started.set();release.wait(1);return 7
 future=lane.submit('decode',slow)
 assert started.wait(.2)
 assert not future.done()
 # RF caller can continue without require().
 marker=[];marker.append('next-rf')
 assert marker==['next-rf']
 release.set();assert lane.require('decode',1)==7
 lane.close_at_deadline(time.time())

def test_only_explicit_require_waits_for_mandatory_result():
 lane=ProcessingLane(1);release=threading.Event()
 lane.submit('clearance',lambda:(release.wait(1),True)[1])
 done=threading.Event()
 t=threading.Thread(target=lambda:(lane.require('clearance',1),done.set()));t.start()
 assert not done.wait(.05);release.set();assert done.wait(.5);t.join();lane.close_at_deadline(time.time())

def test_duplicate_task_is_rejected():
 lane=ProcessingLane(1);lane.submit('seal',lambda:1)
 with pytest.raises(ValueError,match='duplicate'):lane.submit('seal',lambda:2)
 lane.close_at_deadline(time.time())

def test_recorder_is_reused_across_cells(monkeypatch,tmp_path):
 lane=ProcessingLane();created=[]
 def popen(*a,**k):p=FakeProc();created.append(p);return p
 monkeypatch.setattr('processing_lane.subprocess.Popen',popen)
 one=lane.start_recorder('rx1',['recorder'],tmp_path/'e')
 two=lane.start_recorder('rx1',['different'],tmp_path/'e')
 assert one is two and len(created)==1
 monkeypatch.setattr('processing_lane.os.killpg',lambda *a:None);one._poll=0;lane.close_at_deadline(time.time())

def test_deadline_close_marks_running_work_incomplete_without_waiting(monkeypatch):
 lane=ProcessingLane(1);release=threading.Event();started=threading.Event()
 def slow():started.set();release.wait(5)
 lane.submit('slow-decode',slow);assert started.wait(.2)
 before=time.monotonic();rows=lane.close_at_deadline(time.time());elapsed=time.monotonic()-before
 assert elapsed < .5
 row=next(x for x in rows if x['name']=='slow-decode')
 assert row['status']=='incomplete-at-deadline'
 release.set()

def test_finished_receipt_has_finished_epoch():
 lane=ProcessingLane(1);lane.submit('done',lambda:3);assert lane.require('done',1)==3
 rows=lane.close_at_deadline(time.time());row=next(x for x in rows if x['name']=='done')
 assert row['status']=='finished' and row['finished_epoch'] is not None

def test_component_has_no_radio_control_surface():
 text=(pathlib.Path(__file__).resolve().parents[1]/'src'/'processing_lane.py').read_text()
 for forbidden in ('Rig(', 'rigctl', 'tx-enable', 'allow-tx', 'set_ptt', 'radio freq'):
  assert forbidden not in text
