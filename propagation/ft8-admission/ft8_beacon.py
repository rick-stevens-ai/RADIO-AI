#!/usr/bin/env python3
"""One bounded, telemetry-verified FT8 propagation beacon."""
from __future__ import annotations
import argparse,json,os,pathlib,subprocess,tempfile,time
from hamradio.rig import Rig
from hamradio import tx as txmod
from hamradio.ft8 import encode_wav
DIAL={'80m':3573000,'40m':7074000,'30m':10136000,'20m':14074000,'15m':21074000,'12m':24915000,'10m':28074000}

def validate(band,offset,power):
 if band not in DIAL:raise ValueError('unsupported or safety-blocked TX band')
 if not 300<=int(offset)<=2700:raise ValueError('audio offset outside passband')
 if not 1<=float(power)<=20:raise ValueError('power percent outside bounded profile')
 return {'band':band,'dial_hz':DIAL[band],'audio_offset_hz':int(offset),'rfpower_percent':float(power)}
def guard_args(dry_run,allow_tx):return (True,True) if dry_run else (allow_tx,False)
def number(lines):
 for x in lines:
  try:return float(x.strip())
  except ValueError:pass
 raise RuntimeError('no numeric CAT level')
def atomic(path,doc):
 path.parent.mkdir(parents=True,exist_ok=True);fd,name=tempfile.mkstemp(prefix=f'.{path.name}.',suffix='.tmp',dir=path.parent)
 try:
  with os.fdopen(fd,'w') as f:f.write(json.dumps(doc,indent=2)+'\n');f.flush();os.fsync(f.fileno())
  os.replace(name,path);dfd=os.open(path.parent,os.O_RDONLY|os.O_DIRECTORY)
  try:os.fsync(dfd)
  finally:os.close(dfd)
 finally:
  try:os.unlink(name)
  except FileNotFoundError:pass
def beacon_succeeded(doc,restore_error,post_state):
 return bool(doc.get('transmission_complete') and not restore_error and post_state.get('ptt') is False and post_state.get('tx_enabled') is False)
def main():
 p=argparse.ArgumentParser();p.add_argument('--band',required=True);p.add_argument('--offset',type=int,required=True);p.add_argument('--power',type=float,default=10);p.add_argument('--message',default='CQ KD9NWA EN51');p.add_argument('--run-id',required=True);p.add_argument('--output',type=pathlib.Path,required=True);p.add_argument('--allow-tx',action='store_true');p.add_argument('--dry-run',action='store_true');a=p.parse_args();cfg=validate(a.band,a.offset,a.power)
 if not a.allow_tx and not a.dry_run:raise SystemExit('refusing without --allow-tx')
 if a.dry_run:
  now=time.time();atomic(a.output,{'schema':'ft8-propagation-beacon-v1',**cfg,'run_id':a.run_id,'message':a.message,'started_epoch':now,'finished_epoch':now,'dry_run':True,'validated':True,'rf_performed':False,'sent':False,'transmission_complete':False});return 0
 rig=Rig();orig={'freq_hz':rig.get_freq(),'mode':rig.get_mode(),'rfpower':number(rig._cmd('get_level RFPOWER'))};doc={'schema':'ft8-propagation-beacon-v1',**cfg,'run_id':a.run_id,'message':a.message,'started_epoch':time.time(),'sent':False,'transmission_complete':False,'dry_run':a.dry_run,'pre_state':{'ptt':rig.get_ptt(),'tx_enabled':txmod.tx_globally_enabled(),**orig},'safety_samples':[]};atomic(a.output,doc);player=None;wav=None;rc=2
 try:
  txmod._check_guards(cfg['dial_hz'],a.allow_tx,20,dry_run=False)
  if not txmod.tx_globally_enabled():raise RuntimeError('TX master off')
  wav=encode_wav(a.message,a.offset);rig.set_freq(cfg['dial_hz']);rig.set_mode('PKTUSB',3000);rig._cmd(f"set_level RFPOWER {a.power/100:.4f}")
  now=time.time();slot=(int(now//15)+1)*15;time.sleep(max(0,slot-now)+.03);doc['slot_epoch']=slot
  with txmod.keyed(rig,allow_tx=True,timeout=20):
   player=subprocess.Popen(['pw-play',wav],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL);begin=time.monotonic();missing=0
   while player.poll() is None:
    f,s,alc=rig.get_fwd_power(),rig.get_swr(),rig.get_alc()
    if None in (f,s,alc):
     missing+=1
     if missing>=3 or (not doc['safety_samples'] and time.monotonic()-begin>1):raise RuntimeError('telemetry unavailable')
    else:
     missing=0;doc['safety_samples'].append({'epoch':time.time(),'forward_power_w':f,'swr':s,'alc':alc})
     if f>20 or s>2 or alc>.9:raise RuntimeError('telemetry safety limit')
    time.sleep(.25)
  if player.returncode:raise RuntimeError(f'player exit {player.returncode}')
  if not any(x['forward_power_w']>0 for x in doc['safety_samples']):raise RuntimeError('no positive RF')
  doc['transmission_complete']=True
 except Exception as e:doc['error']=repr(e);rc=2
 finally:
  if player and player.poll() is None:
   try:player.terminate();player.wait(timeout=3)
   except Exception as e:doc['player_cleanup_error']=repr(e)
  try:txmod.watchdog_unkey(rig)
  except Exception as e:doc['unkey_error']=repr(e)
  try:txmod.disable_tx()
  except Exception as e:doc['disable_tx_error']=repr(e)
  restore_error=None
  try:rig._cmd(f"set_level RFPOWER {orig['rfpower']}");rig.set_freq(orig['freq_hz']);rig.set_mode(*orig['mode'])
  except Exception as e:restore_error=repr(e);doc['restore_error']=restore_error
  try:post={'ptt':rig.get_ptt(),'tx_enabled':txmod.tx_globally_enabled(),'freq_hz':rig.get_freq(),'mode':rig.get_mode()}
  except Exception as e:post={'ptt':None,'tx_enabled':None};doc['post_state_error']=repr(e)
  cleanup_error=restore_error or doc.get('player_cleanup_error') or doc.get('unkey_error') or doc.get('disable_tx_error')
  doc.update(finished_epoch=time.time(),post_state=post);doc['sent']=beacon_succeeded(doc,cleanup_error,post)
  if not doc['sent'] and not a.dry_run:rc=2
  atomic(a.output,doc)
 return rc
if __name__=='__main__':raise SystemExit(main())
