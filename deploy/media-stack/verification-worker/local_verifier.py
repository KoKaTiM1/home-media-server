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


SAFE_DIAGNOSTIC_CODES=frozenset((
    'range_mismatch','speed_window_timeout','speed_range_truncated','speed_startup_timeout','speed_read_timeout','speed_throughput_low','speed_sustained_low','missing_media_size','cache_attribution','uncached_range_budget',
    'speed_work_budget','local_work_budget','local_output_bound','local_transport',
    'probe_stalled','decode_stalled','metadata_pending','preparation_budget','subprocess_timeout','unclassified','ffmpeg_resource','ffmpeg_transport','ffmpeg_io',
    'ffmpeg_decoder_missing','ffmpeg_stream_mapping','ffmpeg_missing_index',
    'ffmpeg_invalid_data','ffmpeg_decode_error','ffmpeg_truncated_input',
    'ffmpeg_signal_exit','ffmpeg_nonzero_exit','ffmpeg_stderr_exit'))


from source_failure_diagnostics import ffmpeg_failure_code, record as record_source_failure, sanitize as sanitize_failure_evidence


def failure_diagnostic(error, stage, selected=None):
    """Return bounded diagnostics; attach a source hash only to a speed rejection."""
    typ=type(error).__name__
    category={'SourceTooSlow':'speed_rejected','SourceStalled':'stream_stall','DecodeFailure':'decoder','ResourceCheckpoint':'resource','TimeoutError':'torrent_timeout' if stage=='torrent resolution' else 'stream_stall'}.get(typ,'transport')
    code=getattr(error,'code','unclassified')
    if code not in SAFE_DIAGNOSTIC_CODES:code='unclassified'
    value={'category':category,'failure_type':typ,'phase':stage,'code':code}
    if category=='speed_rejected' and isinstance(selected,dict):
        source_hash=selected.get('hash')
        if isinstance(source_hash,str) and re.fullmatch(r'[a-f0-9]{40}',source_hash.lower()):
            value['source_hash']=source_hash.lower()
    return value


def verify(payload,state,output,budget_seconds=470):
    import app as a
    from verification_title import validate_task
    from verification_media import execute,validate_result
    from reliability import Reliability,Checkpoint,DecodeFailure,SourceStalled
    from local_progress import ProgressProxy
    validate_task(payload);key=payload['key'];state.mkdir(parents=True,exist_ok=True)
    a.DATA=state;a.TORR=os.environ.get('LOCAL_TORRSERVER_URL','http://home-media-verification-torrserver:8090')
    a.torr_headers=lambda:{}
    diagnostic={};lock_wait_seconds=0.0;run_started=time.monotonic()
    def record_failure(error,stage,selected=None):
        record_source_failure(a.BUDGET,error,stage,(selected or {}).get('hash'))
        diagnostic.clear();diagnostic.update(failure_diagnostic(error,stage,selected))
    current_phase='torrent resolution';phase_started=time.monotonic();phase_totals={}
    progress=state/'progress.json'
    def phase(name):
        nonlocal current_phase,phase_started
        now=time.monotonic();phase_totals[current_phase]=phase_totals.get(current_phase,0)+now-phase_started
        current_phase=name;phase_started=now
        tmp=progress.with_suffix('.tmp');tmp.write_text(json.dumps({'phase':name,'updated':time.time()}));tmp.replace(progress)
    a.log=lambda message:None
    deadline=time.monotonic()+max(1,min(470,int(budget_seconds)))
    # Preparation/metadata must share the child's budget, leaving the parent
    # its existing result-receipt margin instead of waiting for a forced kill.
    a.BUDGET.worker_deadline=deadline
    raw_path=state/'raw-samples.json'
    try:raw=json.loads(raw_path.read_text())
    except (OSError,ValueError):raw={}
    class LocalReliability(Reliability):
        def verify(self,*args,**kwargs):
            try:return super().verify(*args,**kwargs)
            except Exception as error:
                selected=args[1] if len(args)>1 else kwargs.get('selected')
                record_failure(error,current_phase,selected);raise
        def source_url(self,route,key,selected,progress=None):return proxy.source(a.upstream(selected),selected['length'])
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
            operation=proxy.operation(command)
            if operation:operation.last=time.monotonic()
            with subprocess.Popen(command,stdout=subprocess.PIPE,stderr=subprocess.PIPE) as proc:
                try:
                    while True:
                        a.remaining(1)
                        if time.monotonic()>=min(end,deadline):raise Checkpoint('Local verification budget','local_work_budget')
                        if operation and operation.stalled():
                            error=SourceStalled('Selected request stopped progressing');error.code='probe_stalled' if name=='video probe' else 'decode_stalled';raise error
                        try:
                            out,err=proc.communicate(timeout=.5)
                            if len(out)>1024*1024:raise Checkpoint('Local output bound','local_output_bound')
                            if operation and operation.error and (operation.error=='range_mismatch' or proc.returncode or err):raise Checkpoint('Selected request rejected',operation.error)
                            diagnostic_code=ffmpeg_failure_code(proc.returncode,err)
                            if diagnostic_code in ('ffmpeg_resource','ffmpeg_transport','ffmpeg_io') or proc.returncode<0:raise Checkpoint('Local resource/transport checkpoint',diagnostic_code)
                            if proc.returncode or err:
                                error=DecodeFailure('Local decoder rejected source');error.code=diagnostic_code;raise error
                            return subprocess.CompletedProcess(command,0,out,b'')
                        except subprocess.TimeoutExpired:continue
                except Exception as error:
                    record_failure(error,name);raise
                finally:
                    if operation:operation.request_stop('verifier_command_finished')
                    if proc.poll() is None:proc.kill()
                    proc.communicate()
        def sample(self,key,selected,info,audio_index,conversion,position,seconds,end):
            video=next(s for s in info['streams'] if s.get('codec_type')=='video' and not s.get('disposition',{}).get('attached_pic'))
            params={'position':position,'seconds':seconds,'video_index':video['index'],'video_codec':video['codec_name'],'audio_index':audio_index,'conversion':conversion}
            value=execute('sample',params,self.source_url('raw',key,selected),lambda cmd,name:self.run(cmd,key,name,selected,None,end),state)
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
    data_root=state.parent.parent
    proxy=ProgressProxy(data_root/'proxy-lifecycle.jsonl',data_root/'proxy-lifecycle-observe-until')
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
        budget_expired=time.monotonic()>=deadline
        if budget_expired:
            record_failure(Checkpoint('Local verification budget','local_work_budget'),current_phase)
        elif not diagnostic or diagnostic.get('failure_type')!=type(error).__name__ or diagnostic.get('code')!=getattr(error,'code','unclassified'):record_failure(error,current_phase)
        # Legacy diagnostic fields deliberately remain operationally identical.
        evidence=sanitize_failure_evidence(getattr(error,'source_failure_evidence',{}))
        if budget_expired:
            # The parent's actual deadline remains terminal even if an older
            # caught rejection or wrapper checkpoint carries richer evidence.
            evidence['root_cause']={k:diagnostic[k] for k in ('category','failure_type','phase','code')}
        diagnostic.update(evidence)
        result={'status':'checkpoint','reason':'transport','diagnostic':diagnostic,'data':{'backend':'windows-torrent'}}
    finally:proxy.close()
    phase_totals[current_phase]=phase_totals.get(current_phase,0)+time.monotonic()-phase_started
    result['metrics']={'phase_seconds':{k:round(v,3) for k,v in phase_totals.items()},'lock_wait_seconds':round(lock_wait_seconds,3),'active_seconds':round(max(0,time.monotonic()-run_started-lock_wait_seconds),3)}
    tmp=output.with_suffix('.tmp');tmp.write_text(json.dumps(result));tmp.replace(output)

if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--payload',required=True,type=Path);p.add_argument('--state',required=True,type=Path);p.add_argument('--output',required=True,type=Path);p.add_argument('--budget-seconds',type=int,default=470);a=p.parse_args()
    verify(json.loads(a.payload.read_text()),a.state,a.output,a.budget_seconds)
