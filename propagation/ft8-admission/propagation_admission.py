#!/usr/bin/env python3
"""Hourly FT8 propagation admission: sweep -> beacon -> PSKReporter -> SDR proof."""
from __future__ import annotations
import argparse,json,pathlib,time

FT8_DIAL={
 '80m':3573000,'60m':5357000,'40m':7074000,'30m':10136000,
 '20m':14074000,'17m':18100000,'15m':21074000,'12m':24915000,'10m':28074000,
}
TX_ELIGIBLE={'80m','40m','30m','20m','17m','15m','12m','10m'}
DEFAULT_BLOCKED={'17m'}
STAGES=['sweep_9_bands','select_best_band','discovery_ft8_tx','pskreporter_poll','rank_sdrs','admit_sdrs','validation_ft8_tx','exact_sdr_decode','publish_ttl']

def score(row):
 return (int(row.get('unique_receivers',0)),int(row.get('decodes',0)),float(row.get('best_snr_db',-99)))

def select_band(rows,blocked=None,tx_eligible=None):
 blocked=set(blocked or ()); eligible=set(tx_eligible or TX_ELIGIBLE)
 usable=[r for r in rows if r.get('band') in eligible-blocked]
 if not usable:raise ValueError('no TX-eligible band')
 return max(usable,key=score)

def timing_budget(bands=9,psk_wait_s=300):
 # Measured 2026-10-04: nine bands, six public SDRs, one aligned FT8 slot
 # per band plus isolated jt9 decoding took 290.9 seconds. Post-beacon decoding
 # overlaps the PSKReporter reporting wait.
 sweep_s=291 if bands<=9 else 291+(bands-9)*32
 pre_tx_s=25+10+20       # local clearance, tuner, recorder quorum
 beacon_and_settle_s=55  # UTC wait + 15 s waveform + capture stabilization
 post_report_s=120       # one query, ranking, manifest/ledger/restore
 total=sweep_s+pre_tx_s+beacon_and_settle_s+psk_wait_s+post_report_s
 return {'bands':bands,'sweep_s':sweep_s,'psk_wait_s':psk_wait_s,'pre_tx_s':pre_tx_s,'beacon_and_settle_s':beacon_and_settle_s,'post_report_s':post_report_s,'total_s':total,'standard_window_s':900,'fast_path_possible':total<=600}

def exact_outbound(tx,row):
 return (row.get('message')==tx.get('message') and abs(float(row.get('slot_epoch',-1))-float(tx.get('slot_epoch',-2)))<0.75 and abs(int(row.get('offset_hz',-9999))-int(tx.get('audio_offset_hz',0)))<=10)

def rf_collision(ps_text):
 needles=('run_application_cell.py','run_live_triarm_campaign.py','key10_remote_tx','audio_cw_remote_tx','ft8_admission_live.py')
 return any(n in ps_text for n in needles)

def build_plan(root,now=None,deadline=None,no_rf=True):
 now=time.time() if now is None else now;deadline=now+900 if deadline is None else deadline
 return {'schema':'ft8-propagation-admission-plan-v1','created_epoch':now,'deadline_epoch':deadline,'window_s':deadline-now,'bands':list(FT8_DIAL),'tx_eligible':sorted(TX_ELIGIBLE),'blocked_bands':sorted(DEFAULT_BLOCKED),'rf_authorized':not no_rf,'stages':[x for x in STAGES if not no_rf or not x.endswith('_tx')],'timing':timing_budget()}

def main():
 p=argparse.ArgumentParser();p.add_argument('--root',type=pathlib.Path,required=True);p.add_argument('--deadline-epoch',type=float);p.add_argument('--no-rf',action='store_true');a=p.parse_args();a.root.mkdir(parents=True,exist_ok=True);plan=build_plan(a.root,deadline=a.deadline_epoch,no_rf=a.no_rf);(a.root/'plan.json').write_text(json.dumps(plan,indent=2,sort_keys=True)+'\n');print(json.dumps(plan,indent=2));return 0
if __name__=='__main__':raise SystemExit(main())
