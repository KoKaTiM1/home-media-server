"""Windows-local full-title jobs. Server owns candidates, evidence acceptance and commits."""
import hashlib
import json
import math
import re
import time
from urllib.parse import urlencode
from pathlib import Path
from reliability import Checkpoint, Reliability, SourceFailure
from verification_media import validate_result
from verification_jobs import WorkerWait

CAPABILITY='local-torrent-v1'
OPERATION='verify_title'

class LocalTitleCheckpoint(Checkpoint):
    def __init__(self,category):
        super().__init__('Windows local title deferred')
        self.failure_category=category if category in ('torrent_timeout','stream_stall','speed_rejected','decoder','resource','coordinator','transport','interrupted','validation') else 'transport'


def magnet(infohash):
    return 'magnet:?'+urlencode({'xt':'urn:btih:'+infohash,'tr':'udp://tracker.opentrackr.org:1337/announce'})

def validate_task(p):
    if not isinstance(p,dict) or p.get('schema')!=1 or not re.fullmatch(r'(movie:tt[0-9]{5,12}|series:tt[0-9]{5,12}:[0-9]+:[0-9]+)',p.get('key','')):raise ValueError('Invalid title job')
    if not isinstance(p.get('meta'),dict) or not isinstance(p.get('candidates'),list) or not 1<=len(p['candidates'])<=20:raise ValueError('Invalid candidates')
    for c in p['candidates']:
        if not re.fullmatch('[a-f0-9]{40}',c.get('infoHash','')):raise ValueError('Invalid hash')
        # The only publisher URL supported for the public canary; all other
        # jobs use public hash magnets, never server credentials/signed URLs.
        if c.get('torrentUrl') not in (None,'https://webtorrent.io/torrents/big-buck-bunny.torrent'):raise ValueError('Invalid publisher source')
        x=c.get('_exact')
        if x and (x.get('hash')!=c['infoHash'] or type(x.get('index')) is not int or x['index']<1 or type(x.get('length')) is not int or x['length']<=0 or not isinstance(x.get('filename'),str)):raise ValueError('Invalid exact file')
    if len(json.dumps(p).encode())>256*1024:raise ValueError('Title job too large')

def payload(g,key):
    kind,imdb,*_=key.split(':')
    meta={k:v for k,v in g['metadata'](kind,imdb).items() if k in ('name','year','id','type','original_title','originalName','releaseInfo','released')}
    candidates=[]
    for c in g['verification_candidates'](key):
        h=c['infoHash'].lower()
        if g['cached']('source-excluded:'+key+':'+h):continue
        x={'infoHash':h,'title':str(c.get('title',''))[:2000],
           'behaviorHints':{'filename':str(c.get('behaviorHints',{}).get('filename',''))[:2000]}}
        if c.get('fileIdx') is not None:x['fileIdx']=int(c['fileIdx'])
        if c.get('torrentUrl')=='https://webtorrent.io/torrents/big-buck-bunny.torrent':x['torrentUrl']=c['torrentUrl']
        if c.get('_exact'):
            e=c['_exact'];x['_exact']={k:e[k] for k in ('hash','index','filename','length')}
            x['_exact']['link']=x.get('torrentUrl') or magnet(h)
        candidates.append(x)
        if len(candidates)>=20:break
    with g['F'].db() as db:
        r=db.execute('SELECT state,proof,reason FROM validation WHERE key=?',(key,)).fetchone()
        m=db.execute('SELECT requested FROM manual_rechecks WHERE key=?',(key,)).fetchone()
        alias=db.execute('SELECT english FROM aliases WHERE imdb=?',(imdb,)).fetchone()
        local=db.execute("SELECT name,original FROM home_titles WHERE imdb=? AND kind=?",(imdb,kind)).fetchone()
    names=[str(v) for v in ((list(local) if local else [])+([alias[0]] if alias else [])) if v]
    p={'schema':1,'key':key,'meta':meta,'aliases':names,'candidates':candidates,
       'prior':{'state':r[0] if r else 'pending','proof':json.loads(r[1]) if r and r[1] else None,'reason':r[2] if r else '', 'requested':m[0] if m else 0}}
    validate_task(p);return p

def audio_selection(info):
    video=next((s for s in info.get('streams',[]) if s.get('codec_type')=='video' and not s.get('disposition',{}).get('attached_pic')),None)
    if not video or video.get('codec_name') not in ('h264','mpeg4','vp8','vp9') or (video.get('codec_name')=='h264' and video.get('pix_fmt') not in ('yuv420p','yuvj420p')):raise ValueError('Unsupported video')
    ordered=sorted([s for s in info['streams'] if s.get('codec_type')=='audio'],key=lambda s:0 if s.get('codec_name')=='aac' and s.get('profile')=='LC' else 1)
    audio=next((s for lang in ('eng','en','heb','he') for s in ordered if s.get('tags',{}).get('language')==lang),ordered[0] if ordered else None)
    return video,audio,audio is not None and (audio.get('codec_name')!='aac' or audio.get('profile')!='LC')

def validate_evidence(g,key,p,result,started):
    """Recheck exact identity, raw frame evidence, reuse and speed arithmetic."""
    validate_task(p)
    if p['key']!=key or not isinstance(result,dict):raise ValueError('Wrong title')
    selected=result['selected'];proof=result['proof'];info=validate_result('probe',{}, {'info':result['info']})
    if selected.get('local') or not isinstance(selected.get('filename'),str) or type(selected.get('length')) is not int or selected['length']<=0 or type(selected.get('index')) is not int or selected['index']<1:raise ValueError('Invalid selected file')
    candidates=[c for c in p['candidates'] if c['infoHash']==selected.get('hash')]
    if len(candidates)!=1:raise ValueError('Unassigned source')
    candidate=candidates[0];exact=candidate.get('_exact')
    if exact:
        if any(selected.get(k)!=exact[k] for k in ('hash','index','filename','length')):raise ValueError('Exact source changed')
    elif not g['candidate_matches']({**candidate,'behaviorHints':{'filename':selected['filename']}},key):raise ValueError('Title file mismatch')
    if Path(selected['filename']).suffix.lower() not in g['VIDEO'] or re.search(r'\b(sample|trailer|preview)\b',selected['filename'],re.I):raise ValueError('Wrong media file')
    if g['REL'].excluded(key,selected):raise ValueError('Excluded source')
    files=result.get('files')
    if not isinstance(files,list) or not 1<=len(files)<=5000:raise ValueError('Missing file metadata')
    if exact:
        matches=[f for f in files if f.get('id')==selected['index'] and f.get('path')==selected['filename'] and f.get('length')==selected['length']]
        if len(matches)!=1:raise ValueError('Exact file metadata mismatch')
    else:
        chosen=g['choose_file'](files,key,candidate.get('fileIdx'),candidate.get('behaviorHints',{}).get('filename'))
        if (chosen['id'],chosen['path'],chosen['length'])!=(selected['index'],selected['filename'],selected['length']):raise ValueError('File selection mismatch')
    video,audio,conversion=audio_selection(info);audio_index=audio['index'] if audio else None
    expected={'version':6,'source_id':g['REL'].sid(selected),'source_hash':selected['hash'],'file_index':selected['index'],'filename':selected['filename'],'length':selected['length'],'selected_audio_index':audio_index,'audio_conversion':conversion,'audio_policy':2,'video_copy':True}
    if any(proof.get(k)!=v for k,v in expected.items()):raise ValueError('Proof identity mismatch')
    if not started-5<=proof.get('checked_at',0)<=time.time()+5:raise ValueError('Stale proof')
    duration=float(info.get('format',{}).get('duration') or 0)
    if not math.isfinite(duration) or not 10<=duration<=7*86400:raise ValueError('Invalid duration')
    if proof.get('decode_reused'):
        accepted=g['REL'].decode_proof(key,selected,audio_index,conversion)
        if not accepted or proof.get('samples')!=accepted.get('samples') or proof.get('decode_checked_at')!=accepted.get('decode_checked_at',accepted['checked_at']):raise ValueError('Unaccepted decode reuse')
    else:
        sequence=int((p.get('prior',{}).get('proof') or {}).get('audit_sequence',-1))+1
        if proof.get('audit_sequence')!=sequence:raise ValueError('Wrong audit rotation')
        raw=result.get('raw_samples')
        if not isinstance(raw,list) or len(raw)!=2:raise ValueError('Missing raw samples')
        samples=[]
        for n,r in enumerate(raw):
            params=r['params']
            if params.get('video_index')!=video['index'] or params.get('video_codec')!=video['codec_name'] or params.get('audio_index')!=audio_index or params.get('conversion')!=conversion or params.get('seconds')!=(6 if n==0 else 2):raise ValueError('Wrong sample recipe')
            position=params.get('position',-1)
            if (n==0 and position!=0) or (n==1 and position!=round(min(duration-2,max(6,duration*(.25,.5,.75)[sequence%3])),3)):raise ValueError('Wrong sample position')
            samples.append(validate_result('sample',params,r['data']))
        if samples!=proof.get('samples'):raise ValueError('Decoded evidence mismatch')
    if not Reliability.speed_proof_valid(proof):raise ValueError('Incomplete V6 speed evidence')
    speed=proof['speed']
    if not started-5<=speed.get('checked_at',0)<=time.time()+5:raise ValueError('Stale speed measurement')
    required=1.5*selected['length']/duration
    if not math.isclose(speed['required_bytes_per_second'],required,rel_tol=1e-6):raise ValueError('Wrong speed threshold')
    for fraction,w in zip((.35,.70),speed['windows']):
        size=w['bytes'];cached=w['cached_bytes_bound'];seconds=w['seconds']
        if type(size) is not int or not 4*1024**2<=size<=32*1024**2 or type(cached) is not int or not 0<=cached<=size-4*1024**2 or not math.isfinite(seconds) or seconds<=0:raise ValueError('Invalid speed window')
        if w['offset']!=min(selected['length']-size,max(0,int(selected['length']*fraction))) or w['uncached_bytes_lower_bound']!=size-cached or not math.isclose(w['bytes_per_second'],(size-cached)/seconds,rel_tol=1e-6):raise ValueError('Speed attribution mismatch')
    selected={k:selected[k] for k in ('hash','index','filename','length')}
    selected.update(link=candidate.get('torrentUrl') or magnet(selected['hash']),torrent_url=candidate.get('torrentUrl'),source_name=candidate.get('title',''))
    proof={**proof,'verification_worker':'windows','source_backend':'windows-torrent','speed':{**speed,'measurement_worker':'windows'}}
    return selected,info,proof,audio,conversion

def execute_title(g,key,cancel=None):
    first_sweep=bool(getattr(g['BUDGET'],'first_sweep_attempt',False))
    execution_seconds=240 if first_sweep else 500
    previous=getattr(g['BUDGET'],'worker_deadline',None)
    deadline=time.monotonic()+execution_seconds
    g['BUDGET'].worker_deadline=min(previous or deadline,deadline)
    try:return _execute_title(g,key,cancel,execution_seconds)
    finally:g['BUDGET'].worker_deadline=previous

def _execute_title(g,key,cancel,execution_seconds):
    """Return False for old clients; otherwise commit Windows or run Debian fallback."""
    from verification_remote import jobs,producer_phase
    q=jobs(g)
    if not q or not q.setting_read('local_torrent_enabled',False) or not q.setting_read('local_torrent_permitted',False) or not q.local_workers():return False
    if not q.setting_read('local_torrent_continuous',False) and q.setting_read('local_torrent_pilot_remaining',20)<=0:return False
    with g['request_budget'](time.monotonic()+60,cancel):p=payload(g,key)
    started=time.time();deadline=min(time.monotonic()+execution_seconds,getattr(g['BUDGET'],'worker_deadline',float('inf')))
    fallback=False;job=None;consumed=False;failure_category="transport";cancel_code="controller_error"
    with g['validation_attempt'](True),g['request_budget'](deadline,cancel):
        try:
            sid=hashlib.sha256(json.dumps(p,sort_keys=True).encode()).hexdigest()
            job_seconds=max(1,min(execution_seconds,int(deadline-time.monotonic())))
            try:job=q.submit(key,sid,OPERATION,p,'',job_seconds,execution_seconds=job_seconds)
            except WorkerWait:return False
            producer_phase(g,key,'windows local torrent')
            while True:
                g['remaining'](1)
                state,result=q.poll(job)
                if state=='result':
                    if result['status']!='ok':
                        diagnostic=result.get('diagnostic',{})
                        failure_category=diagnostic.get('category','transport') if isinstance(diagnostic,dict) else 'transport'
                        if result.get('reason')=='interrupted':raise Checkpoint('Verification interrupted')
                        fallback=True;break
                    try:selected,info,proof,audio,conversion=validate_evidence(g,key,p,result['data'],started)
                    except (ValueError,KeyError,TypeError,RuntimeError):
                        q.count('windows_local_invalid_results');failure_category='validation';fallback=True;break
                    if cancel and cancel():raise Checkpoint('Verification interrupted')
                    g['REL'].bind(key,selected)
                    g['finish_preparation'](key,True,deadline,selected,info,proof,audio,conversion)
                    with g['F'].db() as db:
                        accepted=db.execute('SELECT state,proof FROM validation WHERE key=?',(key,)).fetchone()
                    if not accepted or accepted[0]!='verified' or json.loads(accepted[1])!=proof:raise Checkpoint('Title report superseded Windows result')
                    q.count('windows_local_titles_verified');consumed=True
                    return True
                if state in ('cancelled','consumed') or time.monotonic()>=deadline:raise Checkpoint('Windows title lease ended')
                time.sleep(.5)
        except TimeoutError:
            cancel_code='controller_budget';raise
        except Checkpoint:
            cancel_code='receipt_interrupted';raise
        except g['ClientCancelled']:
            cancel_code='foreground_interrupted';raise
        except (ValueError,KeyError,TypeError):
            q.count('windows_local_invalid_results');failure_category='validation';fallback=True
        finally:
            if job:q.finish(job,consumed,cancel_code)
    if fallback:
        q.count('windows_local_server_fallbacks');producer_phase(g,key,'server fallback')
        if not q.setting_read('enabled',False):
            q.count('windows_local_fallback_deferred')
            raise LocalTitleCheckpoint(failure_category)
        old=g['BUDGET'].remote_verification;g['BUDGET'].remote_verification=False
        try:
            with g['REL'].lock('windows-local-server-fallback'):
                g['prepared'](key,background=True,cancel=cancel)
        finally:g['BUDGET'].remote_verification=old
        return True
    return False
