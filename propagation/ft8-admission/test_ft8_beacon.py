import importlib.util,pathlib
P=pathlib.Path(__file__).parent/'ft8_beacon.py'
s=importlib.util.spec_from_file_location('b',P);m=importlib.util.module_from_spec(s);s.loader.exec_module(m)

def test_supported_band_contract():
 assert m.validate('20m',1200,10)['dial_hz']==14074000

def test_rejects_60m_and_blocked_17m():
 for b in ('60m','17m'):
  try:m.validate(b,1200,10)
  except ValueError:pass
  else:raise AssertionError(b)

def test_offset_and_power_bounds():
 for args in [('20m',100,10),('20m',1200,21)]:
  try:m.validate(*args)
  except ValueError:pass
  else:raise AssertionError(args)

def test_dry_run_does_not_require_tx_master():
 assert m.guard_args(dry_run=True,allow_tx=False)==(True,True)
 assert m.guard_args(dry_run=False,allow_tx=True)==(True,False)

def test_success_requires_safe_post_state_and_restore():
 base={'transmission_complete':True}
 assert m.beacon_succeeded(base,None,{'ptt':False,'tx_enabled':False})
 assert not m.beacon_succeeded(base,'restore failed',{'ptt':False,'tx_enabled':False})
 assert not m.beacon_succeeded(base,None,{'ptt':True,'tx_enabled':False})
 assert not m.beacon_succeeded(base,None,{'ptt':False,'tx_enabled':True})

def test_dry_receipt_is_side_effect_free(tmp_path,monkeypatch):
 import sys,json
 class ForbiddenRig:
  def __init__(self):raise AssertionError('dry-run constructed Rig')
 monkeypatch.setattr(m,'Rig',ForbiddenRig)
 out=tmp_path/'dry.json'
 monkeypatch.setattr(sys,'argv',['ft8_beacon.py','--band','20m','--offset','1200','--run-id','dry-1','--output',str(out),'--dry-run'])
 assert m.main()==0
 doc=json.loads(out.read_text())
 assert doc['run_id']=='dry-1' and doc['dry_run'] is True
 assert doc['rf_performed'] is False and doc['sent'] is False
