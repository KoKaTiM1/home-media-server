"""Discover and verify optional Russian audio without replacing V6 sources."""
from contextlib import contextmanager
import hashlib
import json
import math
import re
import sqlite3
import time
import urllib.error


RUSSIAN={'ru','rus','russian'}
HEBREW={'he','heb','iw'}
TEXT={'subrip','srt','ass','ssa','webvtt','mov_text','text'}


class RussianAudio:
    def __init__(self,g): self.g=g

    @contextmanager
    def db(self):
        with self.g['F'].db() as c:
            c.execute('''CREATE TABLE IF NOT EXISTS russian_audio_scans(
                key TEXT PRIMARY KEY, checked REAL NOT NULL DEFAULT 0,
                next_try REAL NOT NULL DEFAULT 0, state TEXT NOT NULL DEFAULT 'queued',
                reason TEXT)''')
            c.execute('''CREATE TABLE IF NOT EXISTS russian_audio_alternates(
                key TEXT NOT NULL, base_sid TEXT NOT NULL, sid TEXT NOT NULL,
                audio_index INTEGER NOT NULL, selected TEXT NOT NULL, info TEXT NOT NULL,
                proof TEXT NOT NULL, subtitle TEXT, year INTEGER NOT NULL DEFAULT 0,
                status TEXT NOT NULL, checked REAL NOT NULL, next_try REAL NOT NULL DEFAULT 0,
                reason TEXT, PRIMARY KEY(key,sid,audio_index))''')
            c.execute('CREATE INDEX IF NOT EXISTS russian_audio_due ON russian_audio_scans(next_try,state)')
            c.execute('CREATE INDEX IF NOT EXISTS russian_audio_year ON russian_audio_alternates(status,year DESC)')
            # Keep newest-first candidate scans index-backed; the catalogue
            # can contain a very large number of indexed titles.
            c.execute('CREATE INDEX IF NOT EXISTS russian_audio_titles_year ON home_titles(year DESC,kind,imdb)')
            yield c

    @staticmethod
    def lang(stream):
        return str((stream.get('tags') or {}).get('language','')).casefold()

    @staticmethod
    def exact_proof(proof,selected,audio_index=None):
        try:
            return (proof.get('version')==6 and proof.get('source_id')
                and proof.get('source_hash')==selected.get('hash')
                and proof.get('file_index')==selected.get('index')
                and proof.get('filename')==selected.get('filename')
                and proof.get('length')==selected.get('length')
                and (audio_index is None or proof.get('selected_audio_index')==audio_index)
                and len(proof.get('samples',[]))==2
                and proof['samples'][0].get('video_frames',0)>0
                and proof['samples'][1].get('video_frames',0)>0)
        except (AttributeError,TypeError,KeyError):return False

    def base(self,key):
        with self.db() as c:
            row=c.execute('''SELECT v.proof,v.checked,v.state,p.value,
                    COALESCE(m.requested,0),COALESCE(h.year,0),COALESCE(m.status,'')
                FROM validation v JOIN playback p ON p.key=v.key
                LEFT JOIN manual_rechecks m ON m.key=v.key
                LEFT JOIN home_titles h ON h.imdb=substr(v.key,instr(v.key,':')+1,
                    CASE WHEN instr(substr(v.key,instr(v.key,':')+1),':')=0
                         THEN length(v.key) ELSE instr(substr(v.key,instr(v.key,':')+1),':')-1 END)
                WHERE v.key=? AND v.state='verified' ''',(key,)).fetchone()
        if not row:return None
        try:proof=json.loads(row[0] or '{}');playback=json.loads(row[3] or '{}');selected=playback['selected']
        except (ValueError,KeyError,TypeError):return None
        if (row[6] in ('pending','failed') or proof.get('version')!=6 or proof.get('checked_at',0)<row[4]
            or not self.exact_proof(proof,selected)
            or self.g['REL'].sid(selected)!=proof.get('source_id')
            or not self.g['REL'].speed_proof_valid(proof.get('speed'),selected.get('local',False))
            or self.g['REL'].excluded(key,selected)):
            return None
        return {'proof':proof,'selected':selected,'playback':playback,'checked':row[1],'year':int(row[5] or 0)}

    def _subtitle(self,key,selected,info=None,require_match=True):
        """Return a source-bound HE subtitle, or schedule embedded extraction."""
        ready=self.g['EMBED'].ready(key,selected)
        for sub in ready:
            if sub.get('lang') in ('heb','he','iw') and sub.get('variant') and sub.get('path'):
                return sub
        try:
            found=self.g['external_subtitles'](key,selected['filename'],False,max_hebrew=2)
            for sub in found:
                if sub.get('lang') in HEBREW and sub.get('variant') and sub.get('path'):
                    # A different release may use this subtitle only when the
                    # provider identified a release-name match.
                    if sub.get('release_match') or not require_match:
                        return sub
        except Exception:pass
        return None

    def _has_embedded_hebrew(self,info):
        return any(s.get('codec_type')=='subtitle' and s.get('codec_name') in TEXT and self.lang(s) in HEBREW
                   for s in info.get('streams',[]))

    def _busy(self):
        if self.g['F'].foreground_busy() or self.g['detail_playback_busy']():return True
        try:return any(s.get('NowPlayingItem') for s in self.g['jf']('/Sessions',timeout=2))
        except Exception:return True

    def _queue(self):
        try:
            from verification_remote import jobs
            q=jobs(self.g)
            return q, q.status()
        except Exception:return None,{}

    def _guard(self,mode):
        q,status=self._queue()
        if q is None:return False,status
        health=q.setting_read('health',{})
        reason=q.setting_read('reason','')
        fresh=(isinstance(health,dict) and time.time()-float(health.get('time',0))<=90
               and reason.startswith('Parallel verification permitted:'))
        if not fresh or not q.setting_read('enabled',False):return False,status
        if mode=='extra':return bool(q.setting_read('russian_extra_permitted',False)),status
        return bool(q.setting_read('russian_serial_permitted',False)),status

    def candidate(self):
        now=time.time()
        with self.db() as c:
            titles=c.execute("SELECT imdb,kind FROM home_titles WHERE kind IN ('movie','series') ORDER BY year DESC,kind,imdb")
            def eligible(key):
                return c.execute('''SELECT 1 FROM validation v
                    LEFT JOIN russian_audio_scans s ON s.key=v.key
                    WHERE v.key=? AND v.state='verified'
                      AND json_extract(v.proof,'$.version')=6 AND COALESCE(s.next_try,0)<=?
                      AND NOT EXISTS(SELECT 1 FROM verification_claims x WHERE x.key=v.key AND x.expires>?)
                      AND NOT EXISTS(SELECT 1 FROM verification_holds x WHERE x.key=v.key AND x.expires>?)
                      AND NOT EXISTS(SELECT 1 FROM manual_rechecks x WHERE x.key=v.key AND x.status='pending')''',
                    (key,now,now,now)).fetchone()
            for imdb,kind in titles:
                if kind=='movie':
                    key='movie:'+imdb
                    if eligible(key):return key
                    continue
                # The primary-key range avoids scanning unrelated episodes.
                # A deferred episode is skipped while the next episode/title
                # remains eligible, preserving newest-release priority.
                prefix='series:'+imdb+':'
                episodes=c.execute('SELECT key FROM validation WHERE key>=? AND key<? ORDER BY checked DESC,key',
                    (prefix,prefix[:-1]+';'))
                for (key,) in episodes:
                    if eligible(key):return key
        return None

    def _torrent_source(self,key,candidate,deadline):
        exact=candidate.get('_exact')
        infohash=str((exact or {}).get('hash') or candidate.get('infoHash','')).lower()
        if not re.fullmatch('[a-f0-9]{40}',infohash):return None
        status={}
        try:status=self.g['torr']({'action':'get','hash':infohash},timeout=2)
        except urllib.error.HTTPError as e:
            if e.code!=404:raise
        except Exception:pass
        if not status.get('file_stats'):
            link=(exact or {}).get('link') or (exact or {}).get('torrent_url') or candidate.get('torrentUrl') or candidate.get('torrent_url') or 'magnet:?xt=urn:btih:'+infohash
            self.g['record_background_torrent'](key,infohash,force=True)
            try:status=self.g['torr']({'action':'add','link':link,'save_to_db':False},timeout=3)
            except Exception:return None
        end=min(deadline,time.monotonic()+45)
        while not status.get('file_stats') and time.monotonic()<end:
            self.g['remaining'](1);time.sleep(min(1,max(.05,end-time.monotonic())))
            try:status=self.g['torr']({'action':'get','hash':infohash},timeout=2)
            except Exception:status={}
        if not status.get('file_stats') or status.get('hash',infohash).lower()!=infohash:return None
        if exact:
            return exact if self.g['exact_torrent_file'](status,exact) else None
        try:
            file=self.g['choose_file'](status['file_stats'],key,candidate.get('fileIdx'),
                (candidate.get('behaviorHints') or {}).get('filename'))
        except (ValueError,TypeError):return None
        if key.startswith('movie:') and not self.g['candidate_matches'](
                {**candidate,'behaviorHints':{'filename':file['path']}},key):return None
        return {'hash':infohash,'index':file['id'],'filename':file['path'],'length':file['length'],
                'source_name':candidate.get('title',''),'file_count':len(status['file_stats']),
                'torrent_url':candidate.get('torrentUrl'),'link':candidate.get('torrentUrl') or candidate.get('torrent_url')}

    def _streams(self,key,selected,info):
        audio=[s for s in info.get('streams',[]) if s.get('codec_type')=='audio' and self.lang(s) in RUSSIAN]
        audio.sort(key=lambda s:(not bool(s.get('disposition',{}).get('default')),
            not (s.get('codec_name')=='aac' and s.get('profile','').casefold()=='lc'),
            -(s.get('channels') or 0),s.get('index',9999)))
        return audio

    def _record(self,key,base_sid,selected,info,proof,subtitle,year,status='verified',reason=None,next_try=0):
        sid=self.g['REL'].sid(selected)
        if subtitle:
            subtitle={**subtitle,'source_id':sid}
        with self.db() as c:
            c.execute('''INSERT INTO russian_audio_alternates
                (key,base_sid,sid,audio_index,selected,info,proof,subtitle,year,status,checked,next_try,reason)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(key,sid,audio_index) DO UPDATE SET base_sid=excluded.base_sid,
                  selected=excluded.selected,info=excluded.info,proof=excluded.proof,
                  subtitle=excluded.subtitle,year=excluded.year,status=excluded.status,
                  checked=excluded.checked,next_try=excluded.next_try,reason=excluded.reason''',
                (key,base_sid,sid,proof.get('selected_audio_index'),json.dumps(selected),json.dumps(info),
                 json.dumps(proof),json.dumps(subtitle) if subtitle else None,year,status,time.time(),next_try,reason))

    def verify_audio(self,key,base,selected,info,audio,deadline):
        baseproof=base['proof'];sid=self.g['REL'].sid(selected);idx=audio['index']
        conversion=audio.get('codec_name')!='aac' or str(audio.get('profile','')).casefold()!='lc'
        same=(sid==baseproof.get('source_id') and self.exact_proof(baseproof,selected)
              and self.g['REL'].speed_proof_valid(baseproof.get('speed'),selected.get('local',False)))
        if same:
            duration=float(info.get('format',{}).get('duration') or 0)
            if duration<10 or not math.isfinite(duration):raise self.g['SourceFailure']('No usable audio sample duration')
            later=round(min(duration-2,max(6,duration*.5)),3)
            samples=[]
            with self.g['REL'].lock('torrent:'+selected['hash']):
                for position,seconds in ((0,6),(later,2)):
                    sample=self.g['REL'].sample(key,selected,info,idx,conversion,position,seconds,deadline)
                    samples.append(sample)
            proof={**baseproof,'selected_audio_index':idx,'audio_conversion':conversion,
                'samples':samples,'checked_at':time.time(),'decode_checked_at':time.time(),
                'decode_reused':False,'speed_reused':True,'alternate_audio':True,
                'audio_language':'rus'}
            return proof
        return self.g['REL'].verify(key,selected,info,idx,conversion,deadline)

    def _current_hebrew(self,key,base):
        sub=self._subtitle(key,base['selected'],base['playback'].get('verification_info'),require_match=False)
        return sub

    def once(self,mode='extra'):
        if mode not in ('extra','serial'):return False
        permitted,status=self._guard(mode)
        if not permitted:return False
        if mode=='serial' and status.get('windows_slots_online',0)<1:return False
        if self._busy():return False
        key=self.candidate()
        if not key:return False
        base=self.base(key)
        if not base:return self._defer(key,'base_proof_not_current',3600)
        base_sub=self._current_hebrew(key,base)
        if not base_sub:return self._defer(key,'hebrew_subtitle_not_ready',12*3600)
        token=self.g['F'].claim_verification(key,'russian-audio')
        if not token:return False
        deadline=time.monotonic()+420
        budget=self.g['BUDGET'];old={n:getattr(budget,n,None) for n in ('validation_background','worker_deadline')}
        budget.validation_background=True;budget.worker_deadline=deadline
        checks={'busy_at':0.0,'busy':False,'guard_at':0.0,'guard':False}
        def cancel():
            now=time.monotonic()
            if now-checks['busy_at']>=1:
                checks['busy']=self._busy();checks['busy_at']=now
            if now-checks['guard_at']>=5:
                checks['guard']=not self._guard(mode)[0];checks['guard_at']=now
            return checks['busy'] or checks['guard']
        try:
            with self.g['request_budget'](deadline,cancel):
                # Recheck after acquiring the shared title claim.
                base=self.base(key)
                if not base:return self._defer(key,'base_proof_not_current',3600)
                base_sid=base['proof']['source_id'];base_sub=self._current_hebrew(key,base)
                if not base_sub:return self._defer(key,'hebrew_subtitle_not_ready',12*3600)
                candidates=[];seen=set()
                for candidate in self.g['verification_candidates'](key):
                    h=str(candidate.get('infoHash') or (candidate.get('_exact') or {}).get('hash','')).lower()
                    if h and h not in seen and not (candidate.get('_exact') and candidate['_exact'].get('local')):
                        seen.add(h);candidates.append(candidate)
                    if len(candidates)>=5:break
                for candidate in candidates:
                    self.g['remaining'](1)
                    selected=self._torrent_source(key,candidate,deadline)
                    if not selected:continue
                    sid=self.g['REL'].sid(selected)
                    with self.db() as c:
                        prior=c.execute('SELECT base_sid,status,next_try FROM russian_audio_alternates WHERE key=? AND sid=?',(key,sid)).fetchall()
                    if prior and all(row[0]==base_sid and (row[1]=='verified' or row[2]>time.time()) for row in prior):continue
                    self.g['REL'].bind(key,selected)
                    with self.g['REL'].lock('torrent:'+selected['hash']):
                        info=self.g['probe'](selected,key,deadline)
                    for audio in self._streams(key,selected,info):
                        if sid==base_sid and audio['index']==base['proof'].get('selected_audio_index'):continue
                        same=sid==base_sid
                        subtitle=base_sub if same else self._subtitle(key,selected,info)
                        embedded_hebrew=self._has_embedded_hebrew(info)
                        if not subtitle and not embedded_hebrew:
                            self._record(key,base_sid,selected,info,
                                {'selected_audio_index':audio['index']},None,base['year'],
                                status='subtitle_pending',reason='Hebrew subtitle is not ready for this release',next_try=time.time()+12*3600)
                            continue
                        try:proof=self.verify_audio(key,base,selected,info,audio,deadline)
                        except (self.g['Checkpoint'],self.g['ClientCancelled'],TimeoutError,sqlite3.OperationalError):raise
                        except Exception as error:
                            self._record(key,base_sid,selected,info,{'selected_audio_index':audio['index']},subtitle,
                                base['year'],status='rejected',reason=type(error).__name__,next_try=time.time()+12*3600)
                            continue
                        if not self.exact_proof(proof,selected,audio['index']):
                            self._record(key,base_sid,selected,info,{'selected_audio_index':audio['index']},subtitle,
                                base['year'],status='rejected',reason='proof_binding_mismatch',next_try=time.time()+12*3600)
                            continue
                        self._record(key,base_sid,selected,info,proof,subtitle,base['year'])
                        if not subtitle and embedded_hebrew:
                            try:self.g['EMBED'].enqueue(key,selected,float(info.get('format',{}).get('duration') or 0))
                            except (OSError,ValueError,sqlite3.Error):pass
                        # Request durable metadata only after the alternative
                        # has passed; this does not download the media file.
                        try:self.g['retain_torrent_metadata'](selected)
                        except Exception:pass
                        with self.db() as c:c.execute("INSERT OR REPLACE INTO russian_audio_scans VALUES (?,?,?,'found',NULL)",(key,time.time(),time.time()+30*86400))
                        self.g['log']('Russian audio alternative verified for '+key)
                        return True
                return self._defer(key,'no_verified_hebrew_russian_source',7*86400)
        except (self.g['Checkpoint'],self.g['ClientCancelled'],TimeoutError,sqlite3.Error) as error:
            return self._defer(key,type(error).__name__,900)
        except Exception as error:
            self.g['log']('Russian audio check deferred ('+type(error).__name__+')')
            return self._defer(key,type(error).__name__,3600)
        finally:
            for name,value in old.items():
                if value is None:
                    try:delattr(budget,name)
                    except AttributeError:pass
                else:setattr(budget,name,value)
            self.g['F'].release_verification(key,token)

    def _defer(self,key,reason,seconds):
        now=time.time()
        with self.db() as c:
            c.execute('''INSERT INTO russian_audio_scans(key,checked,next_try,state,reason)
                VALUES (?,?,?,'deferred',?) ON CONFLICT(key) DO UPDATE SET
                checked=excluded.checked,next_try=excluded.next_try,state=excluded.state,reason=excluded.reason''',
                (key,now,now+seconds,str(reason)[:100]))
        return False

    def work(self,mode='extra'):
        with (self.g['DATA']/'russian-audio.lock').open('a') as lock:
            import fcntl
            try:fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
            except BlockingIOError:return False
            return self.once(mode)

    def options(self,key,current_proof,current_selected):
        if (not self.exact_proof(current_proof,current_selected)
            or self.g['REL'].sid(current_selected)!=current_proof.get('source_id')):return []
        current=self.base(key)
        if (not current or current['proof']!=current_proof
            or self.g['REL'].sid(current['selected'])!=current_proof.get('source_id')):return []
        base_sid=current_proof.get('source_id')
        with self.db() as c:
            rows=c.execute('''SELECT sid,audio_index,selected,info,proof,subtitle
                FROM russian_audio_alternates WHERE key=? AND base_sid=? AND status='verified'
                ORDER BY checked DESC LIMIT 4''',(key,base_sid)).fetchall()
        output=[]
        for sid,audio_index,raw_selected,raw_info,raw_proof,raw_sub in rows:
            try:
                selected=json.loads(raw_selected);info=json.loads(raw_info);proof=json.loads(raw_proof)
                if (self.g['REL'].sid(selected)!=sid or not self.exact_proof(proof,selected,audio_index)
                    or self.g['REL'].excluded(key,selected)):continue
                if not self.g['REL'].speed_proof_valid(proof.get('speed'),selected.get('local',False)):continue
                sub=json.loads(raw_sub) if raw_sub else None
                if sub and sub.get('source_id')!=sid:sub=None
                if not sub or not sub.get('path') or not self.g['Path'](sub['path']).is_file():
                    subs=self.g['EMBED'].ready(key,selected)
                    sub=next((s for s in subs if s.get('lang') in HEBREW
                              and s.get('source_id')==sid),None)
                if not sub or not sub.get('path') or not self.g['Path'](sub['path']).is_file():continue
                tracks=[]
                for stream in info.get('streams',[]):
                    typ=stream.get('codec_type')
                    if typ not in ('video','audio'):continue
                    lang=self.lang(stream)
                    lang={'he':'heb','iw':'heb','en':'eng','ru':'rus'}.get(lang,lang)
                    tracks.append({'Index':stream['index'],'Type':typ.title(),'Codec':stream.get('codec_name'),
                        'Profile':stream.get('profile'),'Language':lang,'Channels':stream.get('channels'),
                        'BitRate':int(stream['bit_rate']) if str(stream.get('bit_rate','')).isdigit() else None,
                        'SampleRate':int(stream['sample_rate']) if str(stream.get('sample_rate','')).isdigit() else None,
                        'Width':stream.get('width'),'Height':stream.get('height'),'IsExternal':False,
                        'IsDefault':bool(stream.get('disposition',{}).get('default')),
                        'IsForced':bool(stream.get('disposition',{}).get('forced'))})
                subtitle_index=max((s['Index'] for s in tracks),default=-1)+1
                variant=sub.get('variant')
                tracks.append({'Index':subtitle_index,'Type':'Subtitle','Codec':'srt','Language':'heb',
                    'IsExternal':True,'SupportsExternalStream':True,'DeliveryMethod':'External',
                    'DeliveryUrl':self.g['signed_path']('sub',key)+'&lang=heb&variant='+variant,
                    'Title':'עברית 1'})
                audio=next((s for s in tracks if s['Type']=='Audio' and s['Index']==audio_index),None)
                if not audio or audio['Language'] not in RUSSIAN:continue
                duration=float(info.get('format',{}).get('duration') or 0)
                output.append({'Name':'Russian audio · V6 verified','PlaybackUrl':self.g['REL'].source_url('play',key,selected),
                    'SourceId':sid,
                    'MediaStreams':tracks,'Container':self.g['Path'](selected['filename']).suffix[1:].lower(),
                    'RunTimeTicks':int(duration*10000000),'Bitrate':int(info.get('format',{}).get('bit_rate',5000000)),
                    'DefaultSubtitleStreamIndex':subtitle_index,'DefaultAudioStreamIndex':audio_index,
                    'AudioConversionRequired':bool(proof.get('audio_conversion'))})
            except (OSError,ValueError,TypeError,KeyError,sqlite3.Error):continue
        return output

    def prepare_option(self,key,sid):
        base=self.base(key)
        if not base:return None
        for option in self.options(key,base['proof'],base['selected']):
            if option.get('SourceId')!=sid:continue
            with self.db() as c:
                row=c.execute('''SELECT selected,info,proof FROM russian_audio_alternates
                    WHERE key=? AND base_sid=? AND sid=? AND status='verified'
                    ORDER BY checked DESC LIMIT 1''',(key,base['proof']['source_id'],sid)).fetchone()
            if not row:continue
            try:
                selected=json.loads(row[0]);info=json.loads(row[1]);proof=json.loads(row[2])
                return {**option,'selected':selected,'verification_info':info,
                    'verification_version':6,'audio_policy':2,'subtitle_policy':5,
                    'checked_at':proof['checked_at']}
            except (ValueError,TypeError,KeyError):return None
        return None

