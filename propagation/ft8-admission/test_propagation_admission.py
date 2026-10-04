import importlib.util,json,pathlib
HERE=pathlib.Path(__file__).parent
spec=importlib.util.spec_from_file_location('pa',HERE/'propagation_admission.py')
assert spec and spec.loader
pa=importlib.util.module_from_spec(spec);spec.loader.exec_module(pa)

def test_all_nine_routine_bands_are_swept():
 assert list(pa.FT8_DIAL)==['80m','60m','40m','30m','20m','17m','15m','12m','10m']
 assert len(set(pa.FT8_DIAL.values()))==9

def test_selection_excludes_receive_only_and_safety_blocked_bands():
 rows=[
  {'band':'17m','unique_receivers':9,'decodes':40,'best_snr_db':10},
  {'band':'60m','unique_receivers':8,'decodes':30,'best_snr_db':5},
  {'band':'40m','unique_receivers':4,'decodes':8,'best_snr_db':-2},
 ]
 pick=pa.select_band(rows,blocked={'17m'},tx_eligible={'80m','40m','30m','20m','15m','12m','10m'})
 assert pick['band']=='40m'

def test_timing_budget_matches_measured_nine_band_sweep():
 fast=pa.timing_budget(9,psk_wait_s=120)
 assert fast['sweep_s']==291
 assert fast['total_s']==641
 assert fast['fast_path_possible'] is False
 standard=pa.timing_budget(9,psk_wait_s=300)
 assert standard['total_s']==821
 assert standard['total_s']<=900
 assert standard['standard_window_s']==900

def test_exact_match_requires_message_slot_and_offset():
 tx={'message':'CQ KD9NWA EN51','slot_epoch':1000.0,'audio_offset_hz':1500}
 row={'message':'CQ KD9NWA EN51','slot_epoch':1000.0,'offset_hz':1498}
 assert pa.exact_outbound(tx,row)
 assert not pa.exact_outbound(tx,{**row,'message':'CQ OTHER EN51'})
 assert not pa.exact_outbound(tx,{**row,'slot_epoch':1015.0})
 assert not pa.exact_outbound(tx,{**row,'offset_hz':1515})

def test_no_rf_rehearsal_has_no_tx_commands(tmp_path):
 plan=pa.build_plan(tmp_path,now=1000,deadline=1900,no_rf=True)
 assert plan['rf_authorized'] is False
 assert plan['stages'][0]=='sweep_9_bands'
 assert 'discovery_ft8_tx' not in plan['stages']
 assert plan['bands']==list(pa.FT8_DIAL)

def test_collision_guard_detects_active_rf_child():
 ps='python3 run_application_cell.py weft\npython worker.py'
 assert pa.rf_collision(ps)
 assert not pa.rf_collision('python3 propagation_admission.py --no-rf')
