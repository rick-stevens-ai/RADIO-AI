import importlib.util,json,os,pathlib,subprocess
HERE=pathlib.Path(__file__).parent
spec=importlib.util.spec_from_file_location('runner',HERE/'run_hourly.py')
assert spec and spec.loader
r=importlib.util.module_from_spec(spec);spec.loader.exec_module(r)

def test_band_score_prefers_actual_decodes_then_paths():
 rows=[{'band':'40m','n_decodes':2,'unique_calls':2,'best_snr_db':-5,'clear_receivers':5},{'band':'20m','n_decodes':5,'unique_calls':4,'best_snr_db':-15,'clear_receivers':2}]
 assert r.choose_band(rows,blocked={'17m'})['band']=='20m'

def test_band_score_excludes_60m_and_17m_block():
 rows=[{'band':'60m','n_decodes':20,'unique_calls':9,'best_snr_db':10,'clear_receivers':6},{'band':'17m','n_decodes':15,'unique_calls':8,'best_snr_db':8,'clear_receivers':6},{'band':'40m','n_decodes':1,'unique_calls':1,'best_snr_db':-10,'clear_receivers':2}]
 assert r.choose_band(rows,blocked={'17m'})['band']=='40m'
 assert [x['band'] for x in r.ranked_bands(rows,blocked={'17m'})]==['40m']

def test_hour_key_is_idempotent():
 assert r.hour_key(1791142800)=='20261004T19'

def test_psk_filter_requires_exact_beacon_window_band_and_mode():
 rows=[{'mode':'FT8','freq_hz':7074000,'flow_start':1000},{'mode':'WSPR','freq_hz':7074000,'flow_start':1001},{'mode':'FT8','freq_hz':14074000,'flow_start':1002},{'mode':'FT8','freq_hz':7074000,'flow_start':800},{'mode':'FT8','freq_hz':7074000,'flow_start':1016}]
 x=r.filter_reports(rows,band='40m',tx_epoch=1000)
 assert len(x)==1 and x[0]['flow_start']==1000

def test_choose_first_safely_tuned_ranked_band(monkeypatch):
 rows=[{'band':'20m','unique_calls':9},{'band':'40m','unique_calls':5}]
 attempts=[]
 def tune(band,deadline=None):
  attempts.append(band)
  if band=='20m':raise RuntimeError('no match')
  return {'tuned':True}
 monkeypatch.setattr(r,'tune_band',tune)
 pick,receipt=r.choose_tunable_band(rows,deadline=2000)
 assert pick['band']=='40m' and receipt['tuned'] and attempts==['20m','40m']

def test_state_never_replays_reserved_or_finished(tmp_path):
 state={'hours':[{'key':'20261004T19','status':'reserved'}]}
 assert not r.may_start_hour(state,'20261004T19')
 assert r.may_start_hour(state,'20261004T20')

def test_offset_selection_avoids_observed_lanes():
 assert r.choose_offset([1003,1495,1802])==1200

def test_ranked_sdrs_are_endpoint_unique():
 rows=[{'sdr':{'host':'a','port':8073}},{'sdr':{'host':'a','port':8073}},{'sdr':{'host':'b','port':8073}}]
 assert [(x['host'],x['port']) for x in r.unique_sdrs(rows,6)]==[('a',8073),('b',8073)]

def test_one_beacon_only_source_contract():
 text=(HERE/'run_hourly.py').read_text()
 assert "remote_beacon(root,'discovery'" not in text
 assert text.count("remote_beacon(root,'validation'")==1

def test_atomic_uses_unique_temporary_files_and_fsyncs(tmp_path,monkeypatch):
 seen=[];real_replace=os.replace;real_fsync=os.fsync;fsyncs=[]
 def replace(src,dst):seen.append(pathlib.Path(src).name);return real_replace(src,dst)
 def fsync(fd):fsyncs.append(fd);return real_fsync(fd)
 monkeypatch.setattr(r.os,'replace',replace);monkeypatch.setattr(r.os,'fsync',fsync)
 target=tmp_path/'state.json'
 r.atomic(target,{'n':1});r.atomic(target,{'n':2})
 assert len(set(seen))==2
 assert len(fsyncs)>=4
 assert json.loads(target.read_text())=={'n':2}

def test_remote_collision_is_included(monkeypatch):
 class Result:
  returncode=0;stderr=''
  def __init__(self,out):self.stdout=out
 def fake(cmd,timeout=0,cwd=None):
  return Result('python run_application_cell.py\n') if cmd[:3]==['ssh','-n','rpi-gateway'] else Result('')
 monkeypatch.setattr(r,'sh',fake)
 assert any('run_application_cell.py' in x for x in r.process_collision())

def test_remote_beacon_rejects_failed_ssh_without_copy(tmp_path,monkeypatch):
 calls=[]
 monkeypatch.setattr(r,'process_collision',lambda:[])
 monkeypatch.setattr(r.time,'time',lambda:1000)
 def fake(cmd,timeout=120,cwd=None):
  calls.append(cmd);return subprocess.CompletedProcess(cmd,23,'','ssh failed')
 monkeypatch.setattr(r,'sh',fake)
 try:r.remote_beacon(tmp_path,'discovery','20m',1200,10,2000,'run-1')
 except RuntimeError as e:assert 'SSH failed' in str(e)
 else:raise AssertionError('accepted failed SSH')
 assert not any(c[0]=='scp' for c in calls)

def test_remote_beacon_requires_current_invocation_receipt(tmp_path,monkeypatch):
 monkeypatch.setattr(r,'process_collision',lambda:[])
 monkeypatch.setattr(r.time,'time',lambda:1000)
 def fake(cmd,timeout=120,cwd=None):
  if cmd[0]=='scp':
   pathlib.Path(cmd[-1]).write_text(json.dumps({'sent':True,'run_id':'old','started_epoch':999,'finished_epoch':1001,'post_state':{'ptt':False,'tx_enabled':False}}))
  return subprocess.CompletedProcess(cmd,0,'','')
 monkeypatch.setattr(r,'sh',fake)
 try:r.remote_beacon(tmp_path,'validation','20m',1200,10,2000,'run-1')
 except RuntimeError as e:assert 'receipt' in str(e)
 else:raise AssertionError('accepted stale receipt')

def test_deadline_guard_is_hard():
 try:r.require_time(1000,30,now=971)
 except RuntimeError as e:assert 'deadline' in str(e)
 else:raise AssertionError('accepted operation extending past deadline')
 assert r.require_time(2000,90,now=1000,cap=420)==420
 assert r.require_time(1300,90,now=1000,cap=420)==210

def test_cleanup_stops_every_recorder_even_if_one_stop_fails(monkeypatch):
 calls=[]
 def fake(proc):
  calls.append(proc)
  if proc=='bad':raise RuntimeError('cleanup failure')
 monkeypatch.setattr(r,'stop',fake)
 r.stop_all({'a':'bad','b':'good'})
 assert calls==['bad','good']

def test_station_restore_requires_safe_verified_readback(monkeypatch):
 calls=[]
 class Result:
  returncode=0
  stdout='{"freq_hz":7114500,"mode":"USB","passband_hz":3000,"ptt":false}'
  stderr=''
 monkeypatch.setattr(r,'sh',lambda cmd,timeout=0,cwd=None:(calls.append(cmd) or Result()))
 snap={'freq_hz':7114500,'mode':'USB','passband_hz':3000}
 out=r.restore_station(snap)
 assert out['safe'] is True and out['restored'] is True
 assert calls[0][0:3]==['ssh','-n','rpi-gateway']
