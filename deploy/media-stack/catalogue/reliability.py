"""Versioned playback evidence, exact source leases and durable audit work.

Torrent-wide counters are deliberately excluded from the stall clock: TorrServer
cannot attribute them to a file. The raw proxy records unique delivered ranges
for each individual sample request instead.
"""
import fcntl
from contextlib import contextmanager
import http.client
import socket
import threading
import urllib.parse
import urllib.error
import hashlib
import json
import math
import re
import subprocess
import tempfile
import time
import uuid
from pathlib import Path

VERSION = 5
POLL_SECONDS = 2
STALL_SECONDS = 45
METADATA_SECONDS = 90
WORK_SECONDS = 480
AUDIO_CODECS = {'aac','ac3','eac3','dts','truehd','flac','mp3','mp2','opus','vorbis','pcm_s16le','pcm_s24le'}

class Checkpoint(RuntimeError):
    """Work remains useful or was interrupted; do not record a negative proof."""


class ResourceCheckpoint(Checkpoint):pass
class DecodeFailure(RuntimeError):pass
class MissingVideoFrames(RuntimeError):pass
class MissingSelectedAudio(RuntimeError):pass
class SourceStalled(RuntimeError):pass


def rank(candidate):
    title = candidate.get('title','')+' '+candidate.get('name','')
    def count(*names):
        for name in names:
            value = candidate.get(name)
            if isinstance(value,(int,float)) and value >= 0:return int(value)
        return None
    seeds = count('seeders','seeds')
    if seeds is None:
        match = re.search(r'seeders?\s*[:= ]\s*(\d+)', title, re.I)
        if match:seeds=int(match[1])
    peers = count('peers')
    if peers is None:
        match = re.search(r'👤\s*(\d+)',title)
        if match:peers=int(match[1])
    available = seeds if seeds is not None else peers
    return (available is not None,available if available is not None else -1,
            seeds is not None, bool(re.search('1080',title)),bool(re.search('AAC',title,re.I)))


def evidence(output, seconds, audio):
    text=output.decode(errors='replace') if isinstance(output,bytes) else output
    bases={};kinds={};packets={}
    for line in text.splitlines():
        base=re.match(r'#tb (\d+): (\d+)/(\d+)',line)
        kind=re.match(r'#media_type (\d+): (\w+)',line)
        if base and int(base[3]):bases[int(base[1])]=int(base[2])/int(base[3])
        if kind:kinds[int(kind[1])]=kind[2]
        if line.startswith('#'):continue
        fields=[v.strip() for v in line.split(',')]
        if len(fields)<6:continue
        try:
            stream=int(fields[0]);pts=int(fields[2])*bases[stream];duration=int(fields[3])*bases[stream]
        except (ValueError,KeyError):continue
        if math.isfinite(pts) and math.isfinite(duration) and duration>0:packets.setdefault(stream,[]).append((pts,duration))
    def stream(kind):
        return next((packets.get(i,[]) for i,k in kinds.items() if k==kind),[])
    video=stream('video');sound=stream('audio')
    def enough(values,tolerance):
        if len(values)<2:return False
        # Both covered timestamp span and accumulated decoded duration matter:
        # a discontinuity must not turn a few frames into a six-second pass.
        return (max(t+d for t,d in values)-min(t for t,d in values)>=seconds-tolerance
                and sum(d for _,d in values)>=seconds-tolerance)
    if not enough(video,.25):raise MissingVideoFrames('Missing decoded video frames or timestamps')
    if audio and not enough(sound,.15):raise MissingSelectedAudio('Missing selected audio frames or timestamps')
    return {'video_frames':len(video),'video_first_pts':min(t for t,_ in video),'video_last_pts':max(t+d for t,d in video),
            'audio_frames':len(sound),'audio_first_pts':min((t for t,_ in sound),default=None),
            'audio_last_pts':max((t+d for t,d in sound),default=None)}


class SampleResponse:
    def __init__(self,connection,response,stop):self.connection=connection;self.response=response;self.stop=stop
    def __getattr__(self,name):return getattr(self.response,name)
    def __enter__(self):return self.response
    def __exit__(self,*args):
        self.stop.set();self.response.close();self.connection.close()


class Reliability:
    def __init__(self,g):self.g=g

    def db(self):
        conn=self.g['F'].db()
        conn.execute('CREATE TABLE IF NOT EXISTS source_choices(key TEXT,sid TEXT,selected TEXT,verified REAL DEFAULT 0,excluded REAL DEFAULT 0,preferred INTEGER DEFAULT 0,PRIMARY KEY(key,sid))')
        conn.execute('CREATE TABLE IF NOT EXISTS verification_audit(key TEXT PRIMARY KEY,reason TEXT,scheduled REAL,completed REAL DEFAULT 0,result TEXT,priority INTEGER DEFAULT 0)')
        conn.execute('CREATE TABLE IF NOT EXISTS preparation_work(key TEXT PRIMARY KEY,value TEXT,updated REAL)')
        conn.execute("CREATE INDEX IF NOT EXISTS validation_audit_checked ON validation(checked) WHERE state='verified'")
        conn.execute('CREATE INDEX IF NOT EXISTS verification_audit_due ON verification_audit(completed,priority)')
        return conn

    @contextmanager
    def lock(self,key):
        folder=self.g['DATA']/'preparation-locks';folder.mkdir(parents=True,exist_ok=True)
        with (folder/hashlib.sha256(key.encode()).hexdigest()).open('a') as handle:
            while True:
                self.g['remaining'](1)
                try:fcntl.flock(handle,fcntl.LOCK_EX|fcntl.LOCK_NB);break
                except BlockingIOError:time.sleep(.1)
            try:yield
            finally:fcntl.flock(handle,fcntl.LOCK_UN)

    def sid(self,selected):
        fields=[selected.get(k) for k in ('hash','index','filename','length','local')]
        return hashlib.sha256(json.dumps(fields,separators=(',',':')).encode()).hexdigest()

    def bind(self,key,selected):
        sid=self.sid(selected)
        with self.db() as c:
            c.execute('INSERT OR IGNORE INTO source_choices(key,sid,selected) VALUES (?,?,?)',(key,sid,json.dumps(selected)))
        return sid

    def lookup(self,key,sid):
        if not re.fullmatch('[a-f0-9]{64}',sid):raise ValueError('Invalid source identity')
        with self.db() as c:row=c.execute('SELECT selected FROM source_choices WHERE key=? AND sid=?',(key,sid)).fetchone()
        if not row:raise ValueError('Unknown exact source')
        return json.loads(row[0])

    def source_url(self,route,key,selected,progress=None):
        sid=self.bind(key,selected)
        url=self.g['signed_path'](route,key)
        # Authenticate the complete binding; a title signature cannot select a
        # different torrent/file by changing query arguments.
        url=url.split('?')[0]+'?sig='+self.g['signature'](key+':'+sid)+'&source='+sid
        if progress:url+='&progress='+progress+'&progress_sig='+self.g['signature'](key+':'+sid+':'+progress)
        return url

    def success(self,key,selected):
        sid=self.bind(key,selected)
        with self.db() as c:
            c.execute('UPDATE source_choices SET preferred=0 WHERE key=?',(key,))
            c.execute('UPDATE source_choices SET preferred=1,verified=?,excluded=0 WHERE key=? AND sid=?',(time.time(),key,sid))
            c.execute('DELETE FROM preparation_work WHERE key=?',(key,))

    def exclude(self,key,selected):
        sid=self.bind(key,selected)
        with self.db() as c:c.execute('UPDATE source_choices SET excluded=? WHERE key=? AND sid=?',(time.time()+900,key,sid))

    def choices(self,key):
        # Bootstrap pre-v5 exact sources from durable playback records without
        # accepting the old proof as a fresh verification success.
        with self.db() as c:
            row=c.execute('SELECT value FROM playback WHERE key=?',(key,)).fetchone()
        if row:
            selected=json.loads(row[0]).get('selected',{})
            if selected.get('hash') and selected.get('filename'):
                sid=self.bind(key,selected)
                with self.db() as c:
                    if not c.execute('SELECT 1 FROM source_choices WHERE key=? AND verified>0',(key,)).fetchone():
                        c.execute('UPDATE source_choices SET preferred=1,verified=1 WHERE key=? AND sid=?',(key,sid))
        with self.db() as c:
            rows=c.execute('SELECT selected FROM source_choices WHERE key=? AND verified>0 AND excluded<=? ORDER BY preferred DESC,verified DESC',(key,time.time())).fetchall()
        return [json.loads(r[0]) for r in rows]

    def excluded(self,key,selected):
        with self.db() as c:row=c.execute('SELECT excluded FROM source_choices WHERE key=? AND sid=?',(key,self.sid(selected))).fetchone()
        return bool(row and row[0]>time.time())

    def checkpoint(self,key,**details):
        with self.db() as c:
            row=c.execute('SELECT value FROM preparation_work WHERE key=?',(key,)).fetchone()
            value=json.loads(row[0]) if row else {}
            value.update(details)
            c.execute('INSERT OR REPLACE INTO preparation_work VALUES (?,?,?)',(key,json.dumps(value),time.time()))
        return value

    def delivered(self,token,start,end):
        # Merge overlapping ranges so repeated headers/seeks cannot manufacture
        # useful progress. Token scope includes the exact source and sample.
        with self.g['db']() as c:
            name='sample-progress:'+token
            row=c.execute('SELECT value FROM cache WHERE key=?',(name,)).fetchone()
            value=json.loads(row[0]) if row else {'ranges':[],'bytes':0}
            ranges=sorted(value['ranges']+[[start,end]])
            merged=[]
            for lo,hi in ranges:
                if merged and lo<=merged[-1][1]:merged[-1][1]=max(merged[-1][1],hi)
                else:merged.append([lo,hi])
            size=sum(hi-lo for lo,hi in merged)
            if size>value['bytes']:value['last_useful_progress']=time.time()
            value.update(ranges=merged,bytes=size)
            c.execute('INSERT OR REPLACE INTO cache VALUES (?,?,?)',(name,json.dumps(value),time.time()+3600))

    def open_sample(self,handler,request,timeout):
        # Closing ffmpeg must also close its own blocked TorrServer read. A
        # watcher can interrupt getresponse()/read while waiting for pieces;
        # it never drops the torrent or touches another viewer's connection.
        parsed=urllib.parse.urlsplit(request.full_url)
        connection=http.client.HTTPConnection(parsed.hostname,parsed.port,timeout=timeout)
        stop=threading.Event()
        def watch():
            while not stop.wait(.25):
                if handler.disconnected():
                    try:
                        if connection.sock:connection.sock.shutdown(socket.SHUT_RDWR)
                    except OSError:pass
                    connection.close();return
        watcher=threading.Thread(target=watch,daemon=True);watcher.start()
        try:
            connection.request(request.get_method(),parsed.path+('?' +parsed.query if parsed.query else ''),headers=dict(request.header_items()))
            response=connection.getresponse()
            if response.status>=400:raise urllib.error.HTTPError('sample',response.status,'Sample upstream rejected',response.headers,None)
            return SampleResponse(connection,response,stop)
        except BaseException:
            stop.set();connection.close();raise

    def cache_state(self,selected):
        if selected.get('local'):return {}
        try:
            return self.g['fetch'](self.g['TORR']+'/cache',{'action':'get','hash':selected['hash']},self.g['torr_headers'](),timeout=1) or {}
        except (TimeoutError,OSError,ValueError):return {}

    def buffer_progress(self,cache,selected,seen):
        # A sole new reader belongs to our exclusive torrent sample lease.
        # Reader positions are absolute torrent piece IDs, supplied by
        # TorrServer itself: never sum lexicographically sorted file_stats.
        readers=cache.get('Readers') or []
        if cache.get('Hash','').lower()!=selected['hash'].lower() or len(readers)!=1:return 0,0
        reader=readers[0];position=reader.get('Reader')
        if not isinstance(position,int):return 0,0
        pieces=cache.get('Pieces') or {};ready=0
        for number in range(position,min(position+2,reader.get('End',position)+1)):
            piece=pieces.get(str(number),pieces.get(number,{}))
            size=max(0,int(piece.get('Size',0)))
            seen[number]=max(seen.get(number,0),size)
            if piece.get('Completed'):ready+=size
        return sum(seen.values()),ready

    def run(self,command,key,phase,selected,token,deadline):
        # Separate CLI workers and HTTP handlers share a torrent lease. An
        # existing unrelated reader disables attribution rather than borrowing
        # its progress. Native playback is never stopped to acquire this lock.
        with self.lock('global-verification'),self.lock('torrent:'+selected.get('hash','local')):
            baseline=self.cache_state(selected)
            attributable=bool(baseline.get('Hash')) and not baseline.get('Readers')
            return self.run_locked(command,key,phase,selected,token,deadline,attributable)

    def run_locked(self,command,key,phase,selected,token,deadline,attributable):
        started=last=time.monotonic();previous=(0,0,0);out=b'';err=b'';pieces={};buffer_bytes=0
        with subprocess.Popen(command,stdout=subprocess.PIPE,stderr=subprocess.PIPE) as process:
            try:
                while True:
                    self.g['remaining'](1)
                    if time.monotonic()>=deadline:raise Checkpoint('Preparation checkpoint')
                    try:
                        out,err=process.communicate(timeout=min(POLL_SECONDS,max(.01,deadline-time.monotonic())))
                        if process.returncode<0 or re.search(rb'Cannot allocate memory|Resource temporarily unavailable|pthread_create failed',err,re.I):raise ResourceCheckpoint('Verification resource limit; retry serially')
                        if process.returncode or (command[command.index('-v')+1]=='error' and err):raise DecodeFailure('Playback sample process failed')
                        return subprocess.CompletedProcess(command,process.returncode,out,err)
                    except subprocess.TimeoutExpired as waiting:
                        err=waiting.stderr or b''
                        progress=self.g['cached']('sample-progress:'+token) or {}
                        frames=sum(1 for line in (waiting.stdout or b'').splitlines() if re.match(rb'^\d+,',line))
                        cache=self.cache_state(selected)
                        downloaded,buffer_bytes=self.buffer_progress(cache,selected,pieces) if attributable else (0,0)
                        current=(progress.get('bytes',0),downloaded,frames)
                        now=time.monotonic()
                        if any(now_value>old_value for now_value,old_value in zip(current,previous)):last=now
                        speed=max(0,current[0]-previous[0],current[1]-previous[1])/max(.01,now-started)
                        previous=current;started=now
                        self.checkpoint(key,phase=phase,source_id=self.sid(selected),received_bytes=current[0],
                            decoded_frames=frames,buffer_bytes=buffer_bytes,downloaded_bytes=downloaded,
                            required_buffer='Current selected-file reader pieces' if attributable else 'Selected sample delivered byte ranges',
                            speed=round(speed),last_useful_progress=time.time()-(now-last),
                            state='preparing' if now-last<STALL_SECONDS else 'stalled')
                        if now-last>=STALL_SECONDS:raise SourceStalled('Selected sample stalled')
            finally:
                if process.poll() is None:process.kill()
                process.communicate()

    def sample(self,key,selected,info,audio_index,conversion,position,seconds,deadline):
        token=uuid.uuid4().hex
        source=selected.get('local') or self.source_url('raw',key,selected,token)
        command=['ffmpeg','-nostdin','-v','error','-xerror','-err_detect','explode','-threads','1','-filter_threads','1','-filter_complex_threads','1']
        if position:command+=['-ss',str(position)]
        video=next(s for s in info['streams'] if s.get('codec_type')=='video' and not s.get('disposition',{}).get('attached_pic'))
        command+=['-i',source,'-t',str(seconds),'-map','0:'+str(video['index'])]
        if audio_index is not None:command+=['-map',f'0:{audio_index}']
        command+=['-sn','-dn']
        self.checkpoint(key,phase='later sample' if position else 'beginning sample',source_id=self.sid(selected),
                        sample_position=position,audio='AAC-LC conversion' if conversion else 'direct',state='preparing')
        # Conversion and decode use fresh samples on every audit; a prior
        # converted header is never considered evidence of decoded frames.
        with tempfile.TemporaryDirectory(prefix='verify-',dir=self.g['DATA']) as directory:
            if conversion:
                output=str(Path(directory)/'sample.mkv')
                converted=self.run(command+['-c:v','copy','-c:a','aac','-profile:a','aac_low','-ac','2','-ar','48000',output],
                                   key,'audio conversion',selected,token,deadline)
                # With loglevel info, errors are fail-fast through -xerror.
                checked=self.run(['ffprobe','-v','error','-threads','1','-analyzeduration','60000000','-probesize','16000000','-show_streams','-of','json',output],key,'conversion probe',selected,token,deadline)
                streams=json.loads(checked.stdout).get('streams',[])
                original=video
                if not any(s.get('codec_type')=='video' and s.get('codec_name')==original['codec_name'] for s in streams):raise RuntimeError('Video copy missing')
                if not any(s.get('codec_type')=='audio' and s.get('codec_name')=='aac' and s.get('profile')=='LC' and s.get('channels')==2 for s in streams):raise RuntimeError('AAC-LC stereo missing')
                command=['ffmpeg','-nostdin','-v','error','-xerror','-err_detect','explode','-threads','1','-filter_threads','1','-filter_complex_threads','1','-i',output,'-map','0:v:0','-map','0:a:0']
            result=self.run(command+['-vf','scale=160:-2:flags=fast_bilinear','-c:v','rawvideo','-threads','1']+(['-c:a','pcm_s16le'] if audio_index is not None else [])+['-fps_mode','passthrough','-max_interleave_delta','100000','-f','framecrc','-'],
                            key,'sample decode',selected,token,deadline)
        return {'position':position,'seconds':seconds,**evidence(result.stdout,seconds,audio_index is not None)}

    def verify(self,key,selected,info,audio_index,conversion,deadline):
        if conversion:
            track=next(s for s in info['streams'] if s.get('index')==audio_index)
            if track.get('codec_name') not in AUDIO_CODECS:raise RuntimeError('Unsupported audio conversion')
        duration=float(info.get('format',{}).get('duration') or 0)
        if duration<10 or not math.isfinite(duration):raise RuntimeError('No usable sample duration')
        with self.db() as c:row=c.execute('SELECT proof FROM validation WHERE key=?',(key,)).fetchone()
        prior=json.loads(row[0]) if row and row[0] else {}
        sequence=int(prior.get('audit_sequence',-1))+1
        later=round(min(duration-2,max(6,duration*(.25,.5,.75)[sequence%3])),3)
        samples=[]
        for position,seconds in ((0,6),(later,2)):
            # Partial evidence is a checkpoint, not a cached pass. Resume only
            # for the same source/track/rotation and within this audit window.
            with self.db() as c:work=c.execute('SELECT value,updated FROM preparation_work WHERE key=?',(key,)).fetchone()
            partial=json.loads(work[0]) if work and time.time()-work[1]<86400 else {}
            identity=[self.sid(selected),audio_index,sequence,position,conversion]
            saved=partial.get('sample_checkpoint')
            if saved and saved.get('identity')==identity and time.time()-saved.get('checked_at',0)<3600:
                sample=saved['evidence']
            else:
                sample=self.sample(key,selected,info,audio_index,conversion,position,seconds,deadline)
            samples.append(sample)
            # Keep the beginning proof while waiting for later pieces.
            if position==0:self.checkpoint(key,sample_checkpoint={'identity':identity,'evidence':sample,'checked_at':time.time()})
        return {'version':VERSION,'source_id':self.sid(selected),'source_hash':selected['hash'],
                'file_index':selected.get('index'),'filename':selected['filename'],'length':selected.get('length'),
                'selected_audio_index':audio_index,'audio_conversion':conversion,'video_copy':True,
                'audio_policy':2,'samples':samples,'audit_sequence':sequence,'checked_at':time.time()}

    def schedule(self):
        self.db().close()
        with self.g['F'].db() as c:
            if c.execute("SELECT 1 FROM cache WHERE key='verification-v5-audit'").fetchone():return
            now=time.time()
            rows=c.execute("SELECT key,proof FROM validation WHERE state='verified' OR (state='unavailable' AND proof IS NOT NULL)").fetchall()
            for key,raw in rows:
                proof=json.loads(raw) if raw else {}
                priority=100 if proof.get('version',0)<5 else 10
                meta=c.execute('SELECT meta FROM items WHERE key=?',(':'.join(key.split(':')[:2]),)).fetchone()
                name=(json.loads(meta[0]).get('name','') if meta else '').casefold()
                if 'border 2' in name or 'spider-man' in name or 'spider man' in name:priority=500
                c.execute('INSERT OR IGNORE INTO verification_audit(key,reason,scheduled,priority) VALUES (?,?,?,?)',(key,'initial',now,priority))
            c.execute("INSERT OR REPLACE INTO cache VALUES ('verification-v5-audit',?,?)",(json.dumps({'scheduled':now,'total':len(rows)}),now+10*365*86400))

    def next_audit(self,recurring=False):
        with self.db() as c:
            row=c.execute('SELECT a.key FROM verification_audit a JOIN validation v ON v.key=a.key WHERE a.completed=0 AND (v.state=\'verified\' OR v.next_check<=?) ORDER BY a.priority DESC,COALESCE(json_extract(v.proof,\'$.version\'),0),a.scheduled LIMIT 1',(time.time(),)).fetchone()
            if not row and recurring:row=c.execute("SELECT key FROM validation WHERE state='verified' AND checked<=? ORDER BY checked LIMIT 1",(time.time()-86400,)).fetchone()
        return row

    def audit_result(self,key,ok):
        with self.db() as c:
            c.execute('UPDATE verification_audit SET completed=?,result=? WHERE key=?',(time.time(),'passed' if ok else 'failed',key))
            if not ok:c.execute('DELETE FROM preparation_work WHERE key=?',(key,))
