#!/usr/bin/env python3
"""Build a sealed band/SDR assignment from one FT8 admission artifact."""
import argparse,hashlib,json,pathlib,sys
HERE=pathlib.Path(__file__).resolve().parent
sys.path.insert(0,str(HERE))
from assignment_adapters import ALL_MODES,seal_assignment

def site(row):
 if row.get('site'):return str(row['site'])
 if row.get('lat') is not None and row.get('lon') is not None:return f"{float(row['lat']):.2f},{float(row['lon']):.2f}"
 return str(row.get('host',''))
def main():
 p=argparse.ArgumentParser();p.add_argument('admission',type=pathlib.Path);p.add_argument('--hour-id',required=True);p.add_argument('--run-id',required=True);p.add_argument('--expires-epoch',type=float,required=True);p.add_argument('--output',type=pathlib.Path,required=True);a=p.parse_args();raw=a.admission.read_bytes();d=json.loads(raw);band=d['selected_band']['band'] if isinstance(d['selected_band'],dict) else d['selected_band'];rows=d.get('next_hour_receiver_roster') or d.get('validation_rows') or d['selected_band'].get('rows',[]);sdrs=[];seen=set()
 for i,r in enumerate(rows):
  if not r.get('host') or not r.get('port'):continue
  endpoint=f"{r['host']}:{int(r['port'])}";sk=site(r)
  if endpoint in seen or sk in seen:continue
  seen|={endpoint,sk};sdrs.append({'endpoint':endpoint,'receiver_id':r.get('candidate_id') or r.get('receiver_id') or f'sdr-{i+1:02d}','site':sk})
 if len(sdrs)<2:raise SystemExit('assignment requires at least two unique SDR sites')
 modes={m:{'owner':'offline-harness' if m=='bpsk' else 'mode-executor','checks':['assignment-fresh','band-legal','sdr-quorum','mode-clearance','tuner','telemetry','closeout']} for m in ALL_MODES};payload={'schema':'hourly-15-45-assignment-v1','hour_id':a.hour_id,'run_id':a.run_id,'selected_band':band,'expires_epoch':a.expires_epoch,'assigned_sdrs':sdrs[:8],'source_admission_sha256':hashlib.sha256(raw).hexdigest(),'modes':modes};out=seal_assignment(payload);a.output.parent.mkdir(parents=True,exist_ok=True);a.output.write_text(json.dumps(out,indent=2,sort_keys=True)+'\n');print(json.dumps({'band':band,'sdrs':len(out['assigned_sdrs']),'seal':out['seal']['digest'],'output':str(a.output)},indent=2))
if __name__=='__main__':main()
