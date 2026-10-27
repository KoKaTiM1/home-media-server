"""On-demand exact-source Hebrew extraction. Never publish incomplete tracks.

A worker reads short, overlapping video windows while idle, saving SRT cues only.
Copied video packet timestamps demonstrate window coverage; these are NOT v5
video decode proofs and never update the validation table.
"""
from contextlib import contextmanager
import fcntl
import hashlib
import json
import math
from pathlib import Path
import re
import sqlite3
import tempfile
import time
import urllib.error

TEXT_CODECS={'subrip','srt','ass','ssa','webvtt','mov_text','text'}
WINDOW=120

class SubtitleCheckpoint(RuntimeError):pass


def cues(text):
    output=[]
    for block in re.split(r'\n\s*\n',text.replace('\r\n','\n').strip()):
        lines=block.splitlines()
        if lines and lines[0].isdigit():lines=lines[1:]
        if len(lines)<2:continue
        match=re.fullmatch(r'(\d{2}:\d{2}:\d{2}[,.]\d{3})\s*-->\s*(\d{2}:\d{2}:\d{2}[,.]\d{3})',lines[0].strip())
        if not match:raise ValueError('Invalid subtitle cue')
        def seconds(value):
            h,m,s=value.replace(',','.').split(':');return int(h)*3600+int(m)*60+float(s)
        start,end=map(seconds,match.groups())
        if end<=start:raise ValueError('Invalid subtitle duration')
        output.append((start,end,'\n'.join(lines[1:])))
    return output


def render(values):
    def stamp(value):
        value=round(value*1000);h,value=divmod(value,3600000);m,value=divmod(value,60000);s,ms=divmod(value,1000)
        return f'{h:02}:{m:02}:{s:02},{ms:03}'
    return '\n\n'.join(f'{i}\n{stamp(a)} --> {stamp(b)}\n{text}' for i,(a,b,text) in enumerate(sorted(set(values)),1))+'\n'


def covered(raw,start,end):
    """Check copied-packet coverage without claiming decoded-frame evidence."""
    match=re.search(r'^#tb 0:\s*(\d+)/(\d+)',raw,re.M)
    if not match:raise SubtitleCheckpoint('No packet timeline')
    scale=int(match[1])/int(match[2]);points=[]
    for line in raw.splitlines():
        if not re.match(r'^0,',line):continue
        fields=line.split(',')
        points.append(float(fields[2])*scale)
    points=sorted(set(points))
    if not points or points[0]>start+2 or points[-1]<end-2 or any(b-a>2 for a,b in zip(points,points[1:])):
        raise SubtitleCheckpoint('Incomplete subtitle window')


class EmbeddedSubtitles:
    def __init__(self,g):self.g=g
    @contextmanager
    def db(self):
        with self.g['F'].db() as c:
            c.execute('CREATE TABLE IF NOT EXISTS embedded_subtitles(key TEXT,sid TEXT,selected TEXT,duration REAL,info TEXT,state TEXT,cursor REAL DEFAULT 0,track INTEGER,attempts INTEGER DEFAULT 0,next_try REAL DEFAULT 0,updated REAL,files TEXT,PRIMARY KEY(key,sid))')
            yield c
    def folder(self,key,sid):return self.g['DATA']/'embedded-subtitles'/hashlib.sha256((key+':'+sid).encode()).hexdigest()
    def enqueue(self,key,selected,duration):
        if selected.get('local') or not math.isfinite(duration) or not 10<=duration<=43200:return
        sid=self.g['REL'].sid(selected)
        with self.db() as c:
            c.execute("INSERT OR IGNORE INTO embedded_subtitles(key,sid,selected,duration,state,updated) VALUES (?,?,?,?,'queued',?)",(key,sid,json.dumps(selected),duration,time.time()))
            c.execute("UPDATE embedded_subtitles SET state='queued',next_try=0,updated=? WHERE key=? AND sid=? AND state='stale'",(time.time(),key,sid))
    def ready(self,key,selected):
        if selected.get('local'):return []
        with self.db() as c:row=c.execute('SELECT state,cursor,duration,files FROM embedded_subtitles WHERE key=? AND sid=?',(key,self.g['REL'].sid(selected))).fetchone()
        if not row or row[0]!='ready' or row[1]<row[2]:return []
        value=json.loads(row[3] or '[]')
        return [s for s in value if Path(s['path']).is_file()]
    def current(self,key,sid):
        with self.g['F'].db() as c:
            row=c.execute('SELECT state,proof FROM validation WHERE key=?',(key,)).fetchone()
            manual=c.execute('SELECT status FROM manual_rechecks WHERE key=?',(key,)).fetchone()
        proof=json.loads(row[1] or '{}') if row else {}
        return bool(row and row[0]=='verified' and proof.get('version')==5 and proof.get('source_id')==sid and (not manual or manual[0]=='passed'))
    def resources_busy(self):
        from verification_remote import jobs
        try:
            queue=jobs(self.g)
            if not queue:return False
            with queue.db() as c:
                health=queue.setting(c,'health',{})
                reason=queue.setting(c,'reason','')
            return (time.time()-health.get('time',0)>60 or health.get('available_bytes',0)<256*1024*1024
                    or health.get('torr_working_bytes',0)>.7*health.get('torr_limit',1)
                    or reason.startswith(('TorrServer restart or OOM','Host memory pressure','TorrServer memory pressure','Resource status unavailable')))
        except (OSError,ValueError,sqlite3.Error):return True
    def busy(self):return self.resources_busy() or self.g['F'].foreground_busy() or self.g['detail_playback_busy']()
    def transport(self,selected):
        try:status=self.g['torr']({'action':'get','hash':selected['hash']},timeout=2)
        except urllib.error.HTTPError as error:
            if error.code!=404:raise
            status={}
        if not status.get('file_stats'):
            status=self.g['torr']({'action':'add','link':selected.get('link') or 'magnet:?xt=urn:btih:'+selected['hash'],'save_to_db':False},timeout=2)
        deadline=time.monotonic()+self.g['remaining'](30)
        while not status.get('file_stats') and time.monotonic()<deadline:
            self.g['remaining'](1)
            time.sleep(min(1,self.g['remaining'](1)))
            status=self.g['torr']({'action':'get','hash':selected['hash']},timeout=2)
        if status.get('hash',selected['hash']).lower()!=selected['hash'].lower():raise SubtitleCheckpoint('Torrent identity changed')
        if not any(f.get('id')==selected['index'] and f.get('path')==selected['filename'] and f.get('length')==selected['length'] for f in status.get('file_stats',[])):
            raise SubtitleCheckpoint('Source metadata not ready')
    def probe(self,key,selected):
        source=self.g['REL'].source_url('raw',key,selected)
        result=self.g['run_preflight'](['ffprobe','-v','error','-show_streams','-show_format','-of','json',source],capture_output=True,timeout=self.g['remaining'](30))
        if result.returncode:raise SubtitleCheckpoint('Subtitle source probe incomplete')
        return json.loads(result.stdout)
    def extract(self,key,selected,info,track,start,end):
        source=selected.get('local') or self.g['REL'].source_url('raw',key,selected)
        video=next(s['index'] for s in info['streams'] if s.get('codec_type')=='video' and not s.get('disposition',{}).get('attached_pic'))
        with tempfile.TemporaryDirectory(prefix='embedded-',dir=self.g['DATA']) as directory:
            target=Path(directory)/'chunk.srt'
            command=['ffmpeg','-nostdin','-v','error','-nostats','-threads','1','-copyts','-ss',str(max(0,start-15)),'-i',source,
                     '-map','0:'+str(track),'-c:s','srt','-to',str(end),'-fs','2097152','-f','srt','-y',str(target),
                     '-map','0:'+str(video),'-c:v','copy','-to',str(end),'-fs','2097152','-f','framecrc','-']
            result=self.g['run_preflight'](command,capture_output=True,timeout=self.g['remaining'](95))
            if result.returncode or result.stderr or len(result.stdout)>=2*1024*1024:raise SubtitleCheckpoint('Subtitle window incomplete')
            covered(result.stdout.decode(),start,end)
            if target.stat().st_size>=2*1024*1024:raise SubtitleCheckpoint('Subtitle output bound')
            return [v for v in cues(target.read_text(encoding='utf-8-sig')) if start<=v[0]<end]
    def once(self,deadline):
        if self.busy():return False
        with self.db() as c:
            row=c.execute("SELECT key,sid,selected,duration,info,cursor,track FROM embedded_subtitles e WHERE state='queued' AND next_try<=? AND NOT EXISTS(SELECT 1 FROM verification_holds h WHERE h.key=e.key AND h.expires>?) ORDER BY updated LIMIT 1",(time.time(),time.time())).fetchone()
        if not row:return False
        key,sid,raw,duration,info,start,track=row;selected=json.loads(raw)
        if not self.current(key,sid) or self.g['REL'].excluded(key,selected):
            with self.db() as c:c.execute("UPDATE embedded_subtitles SET state='stale',updated=? WHERE key=? AND sid=?",(time.time(),key,sid))
            return True
        claim=self.g['F'].claim_verification(key,'subtitle')
        if not claim:return False
        state={'check':0,'busy':False,'renew':time.monotonic()}
        def cancel():
            now=time.monotonic()
            if now-state['renew']>=10:
                if not self.g['F'].renew_verification(key,claim):return True
                state['renew']=now
            if now>=state['check']:
                state['busy']=self.busy() or self.g['F'].verification_held(key) or not self.current(key,sid);state['check']=now+1
            return state['busy']
        try:
            with self.g['request_budget'](deadline,cancel),self.g['REL'].lock('torrent:'+selected['hash']):
                self.g['remaining'](1);self.transport(selected)
                if not info:
                    info=self.probe(key,selected)
                    tracks=[s for s in info.get('streams',[]) if s.get('codec_type')=='subtitle' and s.get('codec_name') in TEXT_CODECS and s.get('tags',{}).get('language') in ('heb','he','iw')]
                    if not tracks or abs(float(info.get('format',{}).get('start_time') or 0))>.1:
                        with self.db() as c:c.execute("UPDATE embedded_subtitles SET state='none',updated=? WHERE key=? AND sid=?",(time.time(),key,sid))
                        return True
                    actual=float(info.get('format',{}).get('duration') or 0)
                    if not math.isfinite(actual) or abs(actual-duration)>2:raise SubtitleCheckpoint('Subtitle duration changed')
                    track=tracks[0]['index']
                    with self.db() as c:c.execute('UPDATE embedded_subtitles SET info=?,track=? WHERE key=? AND sid=?',(json.dumps(info),track,key,sid))
                else:info=json.loads(info)
                end=min(duration,start+WINDOW)
                values=self.extract(key,selected,info,track,start,end)
                self.g['remaining'](1)
                folder=self.folder(key,sid);folder.mkdir(parents=True,exist_ok=True)
                target=folder/(str(round(start*1000))+'.json')
                content=json.dumps(values,ensure_ascii=False)
                if sum(p.stat().st_size for p in folder.glob('*.json') if p!=target)+len(content.encode())>2*1024*1024:
                    raise SubtitleCheckpoint('Subtitle cache size bound')
                temporary=target.with_suffix('.tmp');temporary.write_text(content);temporary.replace(target)
                with self.db() as c:c.execute('UPDATE embedded_subtitles SET cursor=?,attempts=0,next_try=0,updated=? WHERE key=? AND sid=?',(end,time.time(),key,sid))
                if end>=duration:
                    combined=[]
                    for part in folder.glob('*.json'):combined.extend(tuple(v) for v in json.loads(part.read_text()))
                    if combined:
                        sub=self.g['store_subtitle'](key,render(combined).encode(),'heb');sub.update(provider='Embedded',source_id=sid)
                        with self.db() as c:
                            c.execute("UPDATE embedded_subtitles SET state='ready',files=?,updated=? WHERE key=? AND sid=?",(json.dumps([sub]),time.time(),key,sid))
                            c.execute('DELETE FROM cache WHERE key=? OR (key>=? AND key<?)',('prepared:'+key,'prepared:'+key+':','prepared:'+key+':\uffff'))
                    else:
                        with self.db() as c:c.execute("UPDATE embedded_subtitles SET state='none',updated=? WHERE key=? AND sid=?",(time.time(),key,sid))
                return True
        except Exception as error:
            delay=15 if time.monotonic()>=deadline-.5 else 300
            with self.db() as c:c.execute('UPDATE embedded_subtitles SET attempts=attempts+1,next_try=?,updated=? WHERE key=? AND sid=?',(time.time()+delay,time.time(),key,sid))
            code=' HTTP '+str(error.code) if isinstance(error,urllib.error.HTTPError) else ''
            self.g['log']('Embedded subtitle checkpoint ('+type(error).__name__+code+')')
            return False
        finally:self.g['F'].release_verification(key,claim)
    def work(self,seconds=110,max_chunks=4):
        self.g['DATA'].mkdir(parents=True,exist_ok=True)
        with (self.g['DATA']/'embedded-subtitles.lock').open('a') as lock:
            try:fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
            except BlockingIOError:return 0
            deadline=time.monotonic()+seconds;done=0
            while done<max_chunks and deadline-time.monotonic()>5:
                if not self.once(deadline):break
                done+=1
            return done
