import pathlib,sys
sys.path.insert(0,str(pathlib.Path(__file__).resolve().parents[1]/'src'))
from receiver_sessions import ReceiverSessionManager
class P:
 def __init__(self,n):self.n=n
 def poll(self):return None
class Lane:
 def __init__(self):self.started=[];self.stopped=[]
 def start_recorder(self,rid,argv,stderr):p=P(len(self.started));self.started.append((rid,argv,stderr,p));return p
 def stop_recorder(self,rid):self.stopped.append(rid)
def builder(row,freq,d):return ['rec',row['candidate_id'],str(freq),str(d)]
def test_same_frequency_reuses_process(tmp_path):
 l=Lane();m=ReceiverSessionManager(l,builder,tmp_path);r={'candidate_id':'a'};a=m.ensure(r,100);b=m.ensure(r,100)
 assert a.process is b.process and len(l.started)==1 and l.stopped==[]
def test_frequency_change_replaces_only_that_receiver(tmp_path):
 l=Lane();m=ReceiverSessionManager(l,builder,tmp_path);r={'candidate_id':'a'};a=m.ensure(r,100);b=m.ensure(r,200)
 assert a.process is not b.process and l.stopped==['a'] and len(l.started)==2
def test_roster_is_prepared_as_batch(tmp_path):
 l=Lane();m=ReceiverSessionManager(l,builder,tmp_path);rows=[{'candidate_id':'a'},{'candidate_id':'b'}];sessions=m.prepare_roster(rows,300)
 assert [s.receiver_id for s in sessions]==['a','b'] and all(s.frequency_hz==300 for s in sessions)
