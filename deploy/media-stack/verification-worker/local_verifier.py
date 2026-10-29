"""Isolated per-title runtime using the production selector and V6 recipes."""
import argparse
from contextlib import contextmanager
import fcntl
import json
import os
from pathlib import Path
import re
import subprocess
import time


def verify(payload,state,output):
    import app as a
    from verification_title import validate_task
    from verification_media import execute,validate_result
    from reliability import Reliability,Checkpoint,DecodeFailure
    validate_task(payload);key=payload['key'];state.mkdir(parents=True,exist_ok=True)
    a.DATA=state;a.TORR=os.environ.get('LOCAL_TORRSERVER_URL','http://home-media-verification-torrserver:8090')
    a.torr_headers=lambda:{}
    diagnostic={};lock_wait_seconds=0.0;run_started=time.monotonic()
    def record_failure(error,stage):
        typ=type(error).__name__
        category={'SourceTooSlow':'speed_rejected','SourceStalled':'stream_stall','DecodeFailure':'decoder','ResourceCheckpoint':'resource','TimeoutError':'torrent_timeout' if stage=='torrent resolution' else 'stream_stall'}.get(typ,'transport')
        diagnostic.update(category=category,failure_type=typ,phase=stage,code=getattr(error,'code','unclassified'))
    current_phase='torrent resolution'
    progress=state/'progress.json'
    def phase(name):
        nonlocal current_phase
        current_phase=name
        tmp=progress.with_suffix('.tmp');tmp.write_text(json.dumps({'phase':name,'updated':time.time()}));tmp.replace(progress)
    a.log=lambda message:None
    deadline=time.monotonic()+470
    raw_path=state/'raw-samples.json'
    try:raw=json.loads(raw_path.read_text())
    except (OSError,ValueError):raw={}
    class LocalReliability(Reliability):
        def verify(self,*args,**kwargs):
            try:return super().verify(*args,**kwargs)
            except Exception as error:
                record_failure(error,current_phase);raise
        def source_url(self,route,key,selected,progress=None):return a.upstream(selected)
        @contextmanager
        def lock(self,name):
            nonlocal lock_wait_seconds
            import hashlib
            folder=Path(os.getenv('LOCAL_TORRENT_STATE','/torrent-state'))/'locks';folder.mkdir(parents=True,exist_ok=True)
            with (folder/hashlib.sha256(name.encode()).hexdigest()).open('a') as f:
                waiting=time.monotonic();previous_phase=current_phase;announced=False
                try:
                    while True:
                        a.remaining(1)
                        try:fcntl.flock(f,fcntl.LOCK_EX|fcntl.LOCK_NB);break
                        except BlockingIOError:
                            if not announced:phase('waiting for backend lock');announced=True
                            time.sleep(.1)
                finally:lock_wait_seconds+=time.monotonic()-waiting
                if announced:phase(previous_phase)
                try:yield
                finally:fcntl.flock(f,fcntl.LOCK_UN)
        def checkpoint(self,key,**details):
            if details.get('phase'):
                p=details['phase'];phase('torrent resolution' if p in ('metadata','metadata_pending') else 'speed verification' if p=='speed verification' else 'title validation')
            return super().checkpoint(key,**details)
        def run(self,command,key,name,selected,token,end):
            phase(name)
            with subprocess.Popen(command,stdout=subprocess.PIPE,stderr=subprocess.PIPE) as proc:
                try:
                    while True:
                        a.remaining(1)
                        if time.monotonic()>=min(end,deadline):raise Checkpoint('Local verification budget','local_work_budget')
                        try:
                            out,err=proc.communicate(timeout=.5)
                            if len(out)>1024*1024:raise Checkpoint('Local output bound','local_output_bound')
                            if proc.returncode<0 or re.search(rb'Cannot allocate memory|Resource temporarily unavailable|pthread_create failed|Connection refused|Connection reset|HTTP error|Server returned|Input/output error',err,re.I):raise Checkpoint('Local resource/transport checkpoint','local_transport')
                            if proc.returncode or err:raise DecodeFailure('Local decoder rejected source')
                            return subprocess.CompletedProcess(command,0,out,b'')
                        except subprocess.TimeoutExpired:continue
                except Exception as error:
                    record_failure(error,name);raise
                finally:
                    if proc.poll() is None:proc.kill()
                    proc.communicate()
        def sample(self,key,selected,info,audio_index,conversion,position,seconds,end):
            video=next(s for s in info['streams'] if s.get('codec_type')=='video' and not s.get('disposition',{}).get('attached_pic'))
            params={'position':position,'seconds':seconds,'video_index':video['index'],'video_codec':video['codec_name'],'audio_index':audio_index,'conversion':conversion}
            value=execute('sample',params,a.upstream(selected),lambda cmd,name:self.run(cmd,key,name,selected,None,end),state)
            evidence=validate_result('sample',params,value)
            raw[str(position)]={'params':params,'data':value}
            tmp=raw_path.with_suffix('.tmp');tmp.write_text(json.dumps(raw));tmp.replace(raw_path)
            return evidence
    a.REL=LocalReliability(a.__dict__)
    a.verification_candidates=lambda k:iter(payload['candidates']) if k==key else iter(())
    parent=':'.join(key.split(':')[:2]);kind,imdb=key.split(':')[:2]
    with a.F.db() as db:
        db.execute('INSERT OR REPLACE INTO items VALUES (?,?,?,?,?)',(parent,kind,imdb,json.dumps(payload['meta']),'[]'))
        aliases=payload.get('aliases',[])
        if aliases:db.execute('INSERT OR REPLACE INTO aliases VALUES (?,?,?)',(imdb,'',aliases[0]))
        if len(aliases)>1:db.execute('INSERT OR REPLACE INTO home_titles(imdb,kind,name,original) VALUES (?,?,?,?)',(imdb,kind,aliases[0],aliases[1]))
        prior=payload.get('prior') or {}
        db.execute('INSERT OR IGNORE INTO validation(key,state,proof,reason) VALUES (?,?,?,?)',(key,prior.get('state','pending'),json.dumps(prior['proof']) if prior.get('proof') else None,prior.get('reason','')))
        if prior.get('requested'):db.execute('INSERT OR IGNORE INTO manual_rechecks(key,requested,status) VALUES (?,?,?)',(key,prior['requested'],'pending'))
    # Start metadata retrieval without any connection to Debian's torrent engine.
    phase('torrent resolution')
    try:
        a.prepared(key,background=True)
        with a.F.db() as db:
            proof=json.loads(db.execute('SELECT proof FROM validation WHERE key=?',(key,)).fetchone()[0])
            playback=json.loads(db.execute('SELECT value FROM playback WHERE key=?',(key,)).fetchone()[0])
        info=playback['verification_info'];info.get('format',{}).pop('filename',None)
        samples=[] if proof.get('decode_reused') else [raw[str(s['position'])] for s in proof['samples']]
        files=a.torr({'action':'get','hash':playback['selected']['hash']},timeout=2).get('file_stats',[])
        result={'status':'ok','data':{'selected':playback['selected'],'info':info,'proof':proof,'raw_samples':samples,'files':files}}
    except Exception as error:
        # Only bounded categories/type names cross the PC boundary.
        if not diagnostic:record_failure(error,current_phase)
        result={'status':'checkpoint','reason':'transport','diagnostic':diagnostic,'data':{'backend':'windows-torrent'}}
    result['metrics']={'lock_wait_seconds':round(lock_wait_seconds,3),'active_seconds':round(max(0,time.monotonic()-run_started-lock_wait_seconds),3)}
    tmp=output.with_suffix('.tmp');tmp.write_text(json.dumps(result));tmp.replace(output)

if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--payload',required=True,type=Path);p.add_argument('--state',required=True,type=Path);p.add_argument('--output',required=True,type=Path);a=p.parse_args()
    verify(json.loads(a.payload.read_text()),a.state,a.output)
