"""LAN-only catalogue and torrent playback adapter. Secrets never enter logs."""
import argparse
import base64
from contextlib import contextmanager
from concurrent.futures import ThreadPoolExecutor
import datetime as dt
from difflib import SequenceMatcher
import hashlib
import hmac
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import io
import json
import mimetypes
import os
from pathlib import Path
import re
import select
import socket
import sqlite3
import subprocess
import threading
import tempfile
import time
import urllib.error
import urllib.parse as U
import urllib.request as R
import uuid
import xml.etree.ElementTree as ET
import zipfile
from features import Features
from metadata_jobs import MetadataJobs, MetadataBusy, from_marker as metadata_coordinator
from reliability import Reliability, Checkpoint, VERSION, rank, METADATA_SECONDS, WORK_SECONDS

DATA = Path(os.getenv('CATALOGUE_DATA', '/data'))
LIBRARY = Path(os.getenv('CATALOGUE_LIBRARY', '/catalogue'))
MEDIA_ROOT = os.getenv('MEDIA_ROOT','/media')
DOWNLOADS_ROOT = os.getenv('DOWNLOADS_ROOT','/downloads')
BASE = os.getenv('PUBLIC_URL', 'http://<SERVER_LAN_IP>:8092')
SECRET = Path(os.getenv('SIGNING_KEY', '/run/secrets/catalogue_signing'))
JFKEY = Path('/run/secrets/jellyfin_api_key')
TORRPASS = Path('/run/secrets/transmission_password')
JF = os.getenv('JELLYFIN_URL', 'http://jellyfin:8096')
TORR = os.getenv('TORRSERVER_URL', 'http://torrserver:8090')
CINEMETA = 'https://v3-cinemeta.strem.io'
PROVIDER = 'https://torrentio.strem.fun'
VIDEO = {'.mkv', '.mp4', '.webm', '.m4v', '.avi', '.ts'}
LOCKS = {}
LOCKS_LOCK = threading.Lock()
POSTER_STYLE = '4'
STREAM_LIMIT = threading.Semaphore(1)
POSTER_LIMIT = threading.Semaphore(3)
POSTER_LOCKS = [threading.Lock() for _ in range(64)]
LIVE_LOCK = threading.Lock()
LIVE = {'key': None, 'touched': 0, 'active': 0}
DETAIL_LIMIT = threading.Semaphore(1)
DETAIL_SECONDS = 12
DETAIL_LOCK = threading.Lock()
DETAIL_RECENT = {}



# One request budget follows nested network/probe operations. It is thread-local
# so a foreground waiter cannot change an in-flight background validation.
BUDGET = threading.local()
PREPARATION_SECONDS = 27
FIRST_DATA_SECONDS = 15

class PlaybackBusy(RuntimeError): pass
class ClientCancelled(RuntimeError): pass
class MetadataPending(Checkpoint): pass


def remaining(maximum):
    cancel=getattr(BUDGET,'cancel',None)
    if cancel and cancel():raise ClientCancelled('Client disconnected')
    deadline=getattr(BUDGET,'deadline',None)
    if deadline is None:return maximum
    left=deadline-time.monotonic()
    if left<=0:raise TimeoutError('Playback deadline exceeded')
    return min(maximum,left)


@contextmanager
def request_budget(deadline,cancel=None):
    previous=(getattr(BUDGET,'deadline',None),getattr(BUDGET,'cancel',None))
    BUDGET.deadline=deadline;BUDGET.cancel=cancel
    try:yield
    finally:BUDGET.deadline,BUDGET.cancel=previous


def read_bounded(response,size,deadline=None):
    """Use short reads so a trickling provider cannot reset the total budget."""
    output=bytearray()
    while len(output)<size:
        timeout=remaining(15)
        if deadline is not None:
            left=deadline-time.monotonic()
            if left<=0:raise TimeoutError('Video data deadline exceeded')
            timeout=min(timeout,left)
        sock=getattr(getattr(getattr(response,'fp',None),'raw',None),'_sock',None)
        if sock is not None:sock.settimeout(timeout)
        chunk=response.read1(min(65536,size-len(output)))
        if not chunk:break
        output.extend(chunk)
    return bytes(output)


def playback_event(key,phase,started,outcome,error=None):
    # Deliberately log identifiers and exception classes only, never exception
    # strings: HTTP and ffmpeg errors can contain signed URLs and credentials.
    if not re.fullmatch(r'(movie:tt\d{5,12}|series:tt\d{5,12}:\d+:\d+)',key):return
    event={'key':key,'phase':phase,'seconds':round(time.monotonic()-started,3),
           'outcome':outcome,'error_type':type(error).__name__ if error else None}
    log('Playback '+json.dumps(event,sort_keys=True))


@contextmanager
def playback_phase(key,phase):
    started=time.monotonic()
    try:
        yield
    except Exception as error:
        playback_event(key,phase,started,'cancelled' if isinstance(error,ClientCancelled) else 'failed',error)
        raise
    else:playback_event(key,phase,started,'ok')


def log(message):
    print(dt.datetime.now(dt.timezone.utc).isoformat(), message, flush=True)


class Connection(sqlite3.Connection):
    def __exit__(self, *args):
        try:
            return super().__exit__(*args)
        finally:
            self.close()


def db():
    DATA.mkdir(parents=True, exist_ok=True)
    # Maintenance retains its normal lock tolerance; playback never spends
    # another 30 seconds waiting for SQLite after its network deadline.
    lock_timeout=1 if getattr(BUDGET,'deadline',None) is not None else 30
    conn = sqlite3.connect(DATA / 'catalogue.sqlite', timeout=lock_timeout, factory=Connection)
    if conn.execute('PRAGMA journal_mode').fetchone()[0]!='wal':
        conn.execute('PRAGMA journal_mode=WAL')
    conn.execute('CREATE TABLE IF NOT EXISTS cache (key TEXT PRIMARY KEY, value TEXT, expires REAL)')
    conn.execute('CREATE TABLE IF NOT EXISTS items (key TEXT PRIMARY KEY, kind TEXT, imdb TEXT, meta TEXT, buckets TEXT)')
    conn.execute('CREATE TABLE IF NOT EXISTS playback (key TEXT PRIMARY KEY, value TEXT, updated REAL)')
    return conn


def cached(key):
    with db() as conn:
        row = conn.execute('SELECT value FROM cache WHERE key=? AND expires>?', (key,time.time())).fetchone()
    return json.loads(row[0]) if row else None


def remember(key, value, ttl):
    with db() as conn:
        conn.execute('INSERT OR REPLACE INTO cache VALUES (?,?,?)', (key,json.dumps(value),time.time()+ttl))
    return value


def fetch(url, data=None, headers=None, timeout=20, raw=False, method=None):
    # Background catalogue-page requests share the helper's provider budget.
    # Per-title metadata calls have already reserved their slot with a lease.
    parsed=U.urlsplit(url)
    provider=parsed.hostname=='v3-cinemeta.strem.io'
    jobs=metadata_coordinator(DATA) if provider and getattr(BUDGET,'metadata_background',False) else None
    if jobs and parsed.path.startswith('/catalog/'):
        until=time.monotonic()+30
        while True:
            wait=jobs.reserve_provider()
            if wait<=0:break
            if wait>10 or time.monotonic()>=until:raise Checkpoint('Metadata provider budget deferred')
            time.sleep(min(wait,remaining(1)))
    body = json.dumps(data).encode() if data is not None else None
    request = R.Request(url, data=body, headers={'User-Agent':'HomeMediaCatalogue/1.0', 'Content-Type':'application/json', **(headers or {})}, method=method)
    try:
        with R.urlopen(request, timeout=remaining(timeout)) as response:
            result = read_bounded(response,12*1024*1024)
            return result if raw else (json.loads(result) if result else None)
    except urllib.error.HTTPError as error:
        if jobs:jobs.provider_backoff(error.code)
        raise


def jf(path, data=None, method=None, timeout=20):
    return fetch(JF+path,data,{'X-Emby-Token':JFKEY.read_text().strip()},method=method,timeout=timeout)


def torr_headers():
    encoded=base64.b64encode(('server:'+TORRPASS.read_text().strip()).encode()).decode()
    return {'Authorization':'Basic '+encoded}


def torr(data,timeout=12):
    return fetch(TORR+'/torrents',data,torr_headers(),timeout=timeout)


def identity(kind, imdb, season=None, episode=None):
    if kind not in ('movie','series') or not re.fullmatch(r'tt\d{5,12}',imdb):
        raise ValueError('Invalid catalogue ID')
    if kind=='series':
        season,episode=int(season),int(episode)
        if not 0 <= season <= 300 or not 1 <= episode <= 2000:
            raise ValueError('Invalid episode')
        return f'series:{imdb}:{season}:{episode}'
    return 'movie:'+imdb


def signature(key):
    return hmac.new(SECRET.read_bytes().strip(),key.encode(),hashlib.sha256).hexdigest()


def signed_path(route,key):
    return f'{BASE}/{route}/{U.quote(key,safe=":")}?sig={signature(key)}'


def metadata(kind,imdb):
    key=f'meta:{kind}:{imdb}'
    saved=cached(key)
    jobs=metadata_coordinator(DATA)
    lease=None
    if saved is None and jobs:
        metadata_import(limit=1,key=kind+':'+imdb)
        saved=cached(key)
    if saved is None and jobs and getattr(BUDGET,'metadata_background',False):
        jobs.enqueue([(kind,imdb,2000)])
        lease=jobs.claim('server','server',kind,imdb)
        if lease is None:
            with F.db() as conn:
                conn.execute('INSERT OR REPLACE INTO metadata_waits VALUES (?,?)',(kind+':'+imdb,time.time()+30))
            raise MetadataPending('Metadata assigned elsewhere or waiting for provider budget')
    if saved is None:
        try:
            saved=fetch(f'{CINEMETA}/meta/{kind}/{imdb}.json',timeout=8)['meta']
            if not isinstance(saved,dict):raise ValueError('Metadata not found')
        except Exception as provider_error:
            if jobs and lease:
                jobs.release(lease,getattr(provider_error,'code',None))
                # A metadata API timeout is not a failed media-source proof.
                if kind=='series':raise Checkpoint('Metadata provider deferred; source result retained') from None
                lease=None
            index=DATA/'imdb/catalogue.sqlite'
            if not index.exists():raise
            conn=sqlite3.connect('file:'+str(index)+'?mode=ro',uri=True)
            try:row=conn.execute('SELECT name,year,original FROM titles WHERE kind=? AND imdb=?',(kind,imdb)).fetchone()
            finally:conn.close()
            if not row or kind=='series':raise
            saved={'id':imdb,'name':row[0],'originalName':row[2],'releaseInfo':str(row[1]),'type':kind,
                'poster':f'https://images.metahub.space/poster/medium/{imdb}/img'}
        if not isinstance(saved,dict):raise ValueError('Metadata not found')
        if jobs and lease:
            result={'result_id':uuid.uuid4().hex,'source':'cinemeta','fetched_at':time.time(),
                    'status':'ok','metadata':saved}
            ack=jobs.receive('server','server',lease,result)
            if ack not in ('accepted','duplicate'):
                raise Checkpoint('Metadata lease expired; work retained for retry')
            metadata_import(limit=1,key=kind+':'+imdb)
            saved=cached(key)
            if saved is None:raise Checkpoint('Metadata import awaiting retry')
        else:remember(key,saved,21600)
    F.record_dates(kind,[saved])
    return F.localize(saved)


def metadata_import(limit=20,key=None,seed=False):
    """Apply durable metadata handoffs. No source proofs/native userdata writes."""
    jobs=metadata_coordinator(DATA)
    if not jobs:return 0
    if seed:
        jobs.seed(DATA/'catalogue.sqlite')
        with F.db() as conn:conn.execute('DELETE FROM metadata_waits WHERE expires<?',(time.time()-3600,))
    applied=0
    for receipt in jobs.pending_imports(limit=limit,key=key):
        kind,imdb=receipt['job_id'].split(':')
        if kind not in ('movie','series') or not re.fullmatch(r'tt\d{5,12}',imdb):
            jobs.imported(receipt['result_id'],'rejected');continue
        meta=json.loads(receipt['metadata'])
        expires=receipt['fetched']+21600
        if (meta.get('id')!=imdb or meta.get('type')!=kind or expires<=time.time()
                or receipt['generation']!=jobs.generation()):
            jobs.imported(receipt['result_id'],'rejected');continue
        with F.db() as conn:
            row=conn.execute('SELECT expires FROM cache WHERE key=?',('meta:'+kind+':'+imdb,)).fetchone()
            if row and row[0]>=expires:outcome='kept_newer'
            else:
                conn.execute('INSERT OR REPLACE INTO cache VALUES (?,?,?)',
                             ('meta:'+kind+':'+imdb,json.dumps(meta,ensure_ascii=False),expires))
                outcome='applied'
            conn.execute('DELETE FROM metadata_waits WHERE key=?',(kind+':'+imdb,))
        # If interrupted between these databases, the cache expiry comparison
        # makes replay a no-op; native import/watch state is never changed here.
        jobs.imported(receipt['result_id'],outcome)
        if outcome=='applied':
            F.record_dates(kind,[meta],apply=False)
            applied+=1
    return applied


def poster_status(kind,imdb):
    with db() as conn:
        prefix=f'{kind}:{imdb}'
        rows=conn.execute('SELECT value FROM playback WHERE key=? OR (key>=? AND key<?)', (prefix,prefix+':',prefix+':'+chr(0x10ffff))).fetchall()
    langs=set()
    availability=cached('availability:'+kind+':'+imdb)
    if availability: langs.update(availability.get('languages',[]))
    if kind=='movie' and cached('wizdom-search:movie:'+imdb): langs.add('heb')
    for row in rows:
        value=json.loads(row[0])
        # Embedded languages describe one selected release, not every release.
        if time.time()-value.get('checked_at',0)<86400:
            langs.update(value.get('languages',[]))
    return 'HE' if 'heb' in langs else 'EN' if 'eng' in langs else '?'


def poster_badges(metas):
    languages={(m.get('type'),m.get('id')):set() for m in metas}
    cached_rows=[];playback_rows=[]
    with db() as conn:
        keys=['availability:'+kind+':'+imdb for kind,imdb in languages if kind and imdb]
        keys+=['wizdom-search:movie:'+imdb for kind,imdb in languages if kind=='movie']
        for start in range(0,len(keys),400):
            batch=keys[start:start+400]
            cached_rows+=conn.execute('SELECT key,value FROM cache WHERE key IN ('+','.join('?' for _ in batch)+') AND expires>?',(*batch,time.time())).fetchall()
        movies=['movie:'+imdb for kind,imdb in languages if kind=='movie']
        for start in range(0,len(movies),400):
            batch=movies[start:start+400]
            playback_rows+=conn.execute('SELECT key,value FROM playback WHERE key IN ('+','.join('?' for _ in batch)+') AND updated>?',(*batch,time.time()-86400)).fetchall()
        for kind,imdb in languages:
            if kind=='series':
                prefix='series:'+imdb+':'
                playback_rows+=conn.execute('SELECT key,value FROM playback WHERE key>=? AND key<? AND updated>?',(prefix,prefix+chr(0x10ffff),time.time()-86400)).fetchall()
    for key,raw in cached_rows:
        parts=key.split(':');identity=(parts[1],parts[2])
        if identity not in languages:continue
        value=json.loads(raw)
        if parts[0]=='availability':languages[identity].update(value.get('languages',[]))
        elif value:languages[identity].add('heb')
    for key,raw in playback_rows:
        identity=tuple(key.split(':')[:2])
        if identity not in languages:continue
        value=json.loads(raw)
        if time.time()-value.get('checked_at',0)<86400:languages[identity].update(value.get('languages',[]))
    return {key:'HE' if 'heb' in langs else 'EN' if 'eng' in langs else '?' for key,langs in languages.items()}


def refresh_subtitle_badges(limit=25):
    """Search subtitle availability without consuming subtitle-download quota."""
    config=subtitle_config()
    budget_key='subtitle-badge-budget:'+dt.date.today().isoformat()
    used=cached(budget_key) or 0
    remaining=max(0,min(limit,200-used))
    with F.db() as conn:
        rows=conn.execute('SELECT kind,imdb,meta FROM discovery ORDER BY rowid DESC LIMIT 1000').fetchall()
        rows+=conn.execute('SELECT kind,imdb,meta FROM items ORDER BY rowid DESC').fetchall()
    checked=0
    for kind,imdb,raw_meta in rows:
        if checked>=remaining: break
        if cached('availability:'+kind+':'+imdb) is not None: continue
        params={'type':'movie' if kind=='movie' else 'episode'}
        params['imdb_id' if kind=='movie' else 'parent_imdb_id']=imdb[2:]
        try:
            languages=[]
            meta=json.loads(raw_meta)
            key='movie:'+imdb
            if kind=='series':
                episodes=[v for v in meta.get('videos',[]) if v.get('season',0)>0 and v.get('episode',0)>0
                          and (v.get('released') or '9999')[:10]<=dt.date.today().isoformat()]
                if not episodes: continue
                sample=min(episodes,key=lambda v:(v['season'],v['episode']))
                key=f"series:{imdb}:{sample['season']}:{sample['episode']}"
            used+=1
            remember(budget_key,used,86400)
            if wizdom_search(key): languages=['heb']
            for lang,iso in [('he','heb'),('en','eng')]:
                if languages or not config.get('api_key'): break
                if used>=200: return
                result=fetch('https://api.opensubtitles.com/api/v1/subtitles?'+U.urlencode({**params,'languages':lang}),
                    headers={'Api-Key':config['api_key'],'User-Agent':'HomeMediaCatalogue v1.0'},timeout=5)
                used+=1
                remember(budget_key,used,86400)
                if any(entry.get('attributes',{}).get('language')==lang for entry in result.get('data',[])):
                    languages=[iso]
                    break
            remember('availability:'+kind+':'+imdb,{'languages':languages},7*86400)
            folder=LIBRARY/('movies' if kind=='movie' else 'shows')/imdb
            make_poster(folder,json.loads(raw_meta),kind,imdb)
            checked+=1
            time.sleep(.5)
        except urllib.error.HTTPError as error:
            if error.code==429:
                remember(budget_key,200,86400)
                log('Subtitle provider rate limit reached; badges deferred')
                break
            log('Subtitle provider rejected request; check local credentials')
            break
        except Exception:
            log('Subtitle availability search deferred')
            break
    if checked: jf('/Library/Refresh',{},method='POST')
    log(f'Subtitle availability badges checked: {checked}; no subtitle files downloaded')


def save_jpeg_atomic(image,path,quality):
    # Metadata imports and thumbnail requests can render the same title in
    # parallel, including across processes. Never share a temporary filename.
    temporary=None
    try:
        with tempfile.NamedTemporaryFile(dir=path.parent,prefix='poster-tmp-',suffix='.jpg',delete=False) as stream:
            temporary=Path(stream.name)
            os.fchmod(stream.fileno(),0o664)
            image.save(stream,format='JPEG',quality=quality)
        temporary.replace(path)
    finally:
        if temporary is not None:temporary.unlink(missing_ok=True)


def make_poster(folder,meta,kind,imdb):
    from PIL import Image,ImageDraw,ImageFont
    original=folder/'poster-original.jpg'
    updated_original=False
    missing=folder/'poster-unavailable-at'
    retry=not missing.exists() or time.time()-missing.stat().st_mtime>6*3600
    if (not original.exists() or missing.exists()) and retry:
        url=meta.get('poster','')
        try:
            if not url.startswith('https://'):raise ValueError('No artwork URL')
            content=fetch(url,raw=True,timeout=3)
            image=Image.open(io.BytesIO(content)).convert('RGB')
            image.thumbnail((360,540))
            save_jpeg_atomic(image,original,82)
            missing.unlink(missing_ok=True)
            updated_original=True
        except Exception:
            # A provider's missing image must not turn a valid catalogue card
            # into a 404 storm. Retry generated placeholders after six hours.
            missing.write_text('Artwork temporarily unavailable')
            if not original.exists():
                image=Image.new('RGB',(360,540),'#20252b')
                draw=ImageDraw.Draw(image)
                font=ImageFont.truetype('/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf',18)
                import textwrap
                label=meta.get('name') or imdb
                for line,text in enumerate(textwrap.wrap(label,width=25)[:8]):
                    draw.text((18,150+line*29),text,font=font,fill='white')
                save_jpeg_atomic(image,original,82)
    if not original.exists(): return
    badge=poster_status(kind,imdb)
    if (folder/'badge.txt').exists() and (folder/'badge.txt').read_text()==badge and (folder/'badge-style.txt').exists() and (folder/'badge-style.txt').read_text()==POSTER_STYLE and not updated_original: return
    image=Image.open(original).convert('RGB')
    draw=ImageDraw.Draw(image)
    font=ImageFont.truetype('/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf',23)
    # TV clients overlay episode counts in a top corner of series posters.
    top=max(8,image.height-48) if kind=='series' else 8
    draw.rounded_rectangle((8,top,83,top+40),radius=6,fill={'HE':'#126b47','EN':'#b3261e','?':'#555555'}[badge])
    draw.text((22,top+4),badge,font=font,fill='white')
    save_jpeg_atomic(image,folder/'poster.jpg',85)
    (folder/'badge.txt').write_text(badge)
    (folder/'badge-style.txt').write_text(POSTER_STYLE)


def trailer_urls(meta):
    """Use provider-supplied trailer identities, never a title-search guess."""
    candidates=[t.get('ytId') for t in meta.get('trailerStreams',[]) if isinstance(t,dict)]
    for trailer in meta.get('trailers',[]):
        if isinstance(trailer,dict): candidates.extend([trailer.get('source'),trailer.get('id')])
    result=[]
    for candidate in candidates:
        if not isinstance(candidate,str): continue
        ident=candidate
        if candidate.startswith('https://'):
            parsed=U.urlsplit(candidate)
            if parsed.hostname in ('youtube.com','www.youtube.com'):
                ident=U.parse_qs(parsed.query).get('v',[''])[0]
            elif parsed.hostname=='youtu.be': ident=parsed.path.strip('/')
            else: continue
        if re.fullmatch(r'[A-Za-z0-9_-]{11}',ident):
            url='https://www.youtube.com/watch?v='+ident
            if url not in result: result.append(url)
    return result[:4]


def nfo(path,root_tag,values,genres=None):
    root=ET.Element(root_tag)
    for key,value in values.items():
        if value is None or value=='': continue
        if key=='imdbid':
            ET.SubElement(root,'uniqueid',{'type':'imdb','default':'true'}).text=str(value)
        elif key=='trailer':
            for url in value:
                ET.SubElement(root,'trailer').text='plugin://plugin.video.youtube/play/?video_id='+U.parse_qs(U.urlsplit(url).query)['v'][0]
        else: ET.SubElement(root,key).text=str(value)
    for genre in genres or []: ET.SubElement(root,'genre').text=genre
    ET.indent(root)
    ET.ElementTree(root).write(path,encoding='utf-8',xml_declaration=True)


def import_item(kind,meta,bucket):
    imdb=meta.get('imdb_id') or meta.get('id')
    if not re.fullmatch(r'tt\d{5,12}',imdb or ''): return False
    if kind=='movie' and meta.get('released') and meta['released'][:10]>dt.date.today().isoformat():
        return False
    name=meta.get('name') or imdb
    year=str(meta.get('releaseInfo') or meta.get('year') or '')[:4]
    folder=LIBRARY/('movies' if kind=='movie' else 'shows')/imdb
    folder.mkdir(parents=True,exist_ok=True)
    with db() as conn:
        row=conn.execute('SELECT buckets FROM items WHERE key=?',(kind+':'+imdb,)).fetchone()
        buckets=set(json.loads(row[0])) if row else set()
        buckets.add(bucket)
        conn.execute('INSERT OR REPLACE INTO items VALUES (?,?,?,?,?)',(kind+':'+imdb,kind,imdb,json.dumps(meta),json.dumps(sorted(buckets))))
    if kind=='movie':
        if not (folder/'movie.strm').exists():
            # The plugin resolves this only at playback; scans never fetch torrents.
            (folder/'movie.strm').write_text('dynamiclibrary://movie/'+imdb+'\n')
        values={'title':name,'year':year,'imdbid':imdb,'plot':meta.get('description'),
                'premiered':str(meta.get('released',''))[:10], 'rating':meta.get('imdbRating'),
                'runtime':re.sub(r'\D','',str(meta.get('runtime',''))) or None,'trailer':trailer_urls(meta)}
        nfo(folder/'movie.nfo','movie',values,meta.get('genres'))
    else:
        nfo(folder/'tvshow.nfo','tvshow',{'title':name,'year':year,'imdbid':imdb,'plot':meta.get('description'),
            'trailer':trailer_urls(meta)},meta.get('genres'))
        for video in meta.get('videos',[]):
            season=video.get('season'); episode=video.get('episode')
            released=video.get('released','')
            if season is None or not episode or not released or released[:10]>dt.date.today().isoformat(): continue
            episode_path=folder/f'Season {int(season):02d}'
            episode_path.mkdir(exist_ok=True)
            stem=f'S{int(season):02d}E{int(episode):02d}'
            file=episode_path/(stem+'.strm')
            if not file.exists(): file.write_text(f'dynamiclibrary://tv/{imdb}/{season}/{episode}\n')
            nfo(episode_path/(stem+'.nfo'),'episodedetails',{'title':video.get('title') or video.get('name') or stem,
                'season':season,'episode':episode,'aired':released[:10],'plot':video.get('overview')})
    make_poster(folder,meta,kind,imdb)
    return True


def refresh_trailers():
    """Backfill parent NFO metadata and queue only parent-item refreshes."""
    with db() as conn: rows=conn.execute('SELECT kind,imdb,meta FROM items').fetchall()
    native=jf('/Items?Recursive=true&IncludeItemTypes=Movie,Series&Fields=ProviderIds,Path&Limit=5000')['Items']
    indexed={(i['Type'],i.get('ProviderIds',{}).get('Imdb')):i['Id'] for i in native
             if i.get('Path','').startswith('/catalogue/')}
    # Known examples first so their detail pages are immediately testable.
    rows.sort(key=lambda row:row[1] not in ('tt0133093','tt0903747'))
    updated=0; available=0
    for kind,imdb,raw in rows:
        urls=trailer_urls(json.loads(raw))
        if not urls: continue
        available+=1
        folder=LIBRARY/('movies' if kind=='movie' else 'shows')/imdb
        path=folder/('movie.nfo' if kind=='movie' else 'tvshow.nfo')
        if not path.is_file(): continue
        tree=ET.parse(path);root=tree.getroot()
        # Jellyfin 10.11's NFO parser recognizes this Kodi YouTube form, not
        # ordinary watch URLs. It converts the IDs back to native RemoteTrailers.
        nfo_urls=['plugin://plugin.video.youtube/play/?video_id='+U.parse_qs(U.urlsplit(url).query)['v'][0]
                  for url in urls]
        old={t.text for t in root.findall('trailer')}
        if all(url in old for url in nfo_urls) and not any(url in old for url in urls): continue
        for element in list(root.findall('trailer')):
            if element.text in urls: root.remove(element)
        for url in nfo_urls:
            if url not in old: ET.SubElement(root,'trailer').text=url
        ET.indent(root)
        temporary=path.with_suffix('.nfo.new')
        tree.write(temporary,encoding='utf-8',xml_declaration=True)
        temporary.replace(path)
        item=indexed.get(('Movie' if kind=='movie' else 'Series',imdb))
        if item:
            jf('/Items/'+item+'/Refresh?Recursive=false&MetadataRefreshMode=FullRefresh&ImageRefreshMode=None&ReplaceAllMetadata=false',{},method='POST')
        updated+=1
        if updated%100==0: log(f'Trailer metadata updated: {updated}')
        time.sleep(.025)
    log(f'Trailer metadata ready: {available} titles with supplied trailers; {updated} NFOs updated')


def sync(movie_limit=1000,show_limit=200):
    for kind,limit in [('movie',movie_limit),('series',show_limit)]:
        count=0; seen=set()
        sources=[('Recent Releases',f'year/genre={dt.date.today().year}'),('Popular','top')]
        # Allocate half to current-year releases and half to popular titles.
        for bucket,catalog in sources:
            budget=limit//2 if bucket=='Recent Releases' else limit-count
            imported=0
            for skip in range(0,limit*3,100):
                if imported>=budget: break
                separator='&' if '/' in catalog else '/'
                url=f'{CINEMETA}/catalog/{kind}/{catalog}{separator}skip={skip}.json'
                try: entries=fetch(url,timeout=30).get('metas',[])
                except Exception:
                    log('Catalogue page unavailable: '+kind+' '+bucket); break
                if not entries: break
                for entry in entries:
                    imdb=entry.get('id','')
                    if imdb in seen: continue
                    seen.add(imdb)
                    try:
                        meta=metadata(kind,imdb)
                        if import_item(kind,meta,bucket):
                            imported+=1; count+=1
                    except Exception as error:
                        log('Import skipped '+imdb+' ('+type(error).__name__+')')
                    if count%25==0: log(f'Imported {kind}: {count}/{limit}')
                    if imported>=budget: break
                    time.sleep(.15)
        log(f'Catalogue ready: {kind}={count}')
    jf('/Library/Refresh',{},method='POST')


def stream_candidates(key):
    # Stable, publisher-distributed open-film source for setup/playback checks.
    # Preserve HTTP web seeds by adding its actual .torrent rather than a magnet.
    open_film={'infoHash':'dd8255ecdc7ca55fb0bbf81323d87062db1f6d1c',
        'torrentUrl':'https://webtorrent.io/torrents/big-buck-bunny.torrent',
        'title':'Big Buck Bunny — publisher web-seeded release',
        'behaviorHints':{'filename':'Big Buck Bunny.mp4'},'_score':999999}
    saved=cached('streams:'+key)
    if saved is not None:
        if not getattr(BUDGET,'speculative',False) and not cached('source-status:'+key):
            remember('source-status:'+key,{'state':'listed' if saved else 'unavailable','checked_at':time.time()},21600)
        if key=='movie:tt1254207' and not cached('open-film-failed') and not any(s['infoHash']==open_film['infoHash'] for s in saved):
            return [open_film]+saved
        return sorted(saved,key=rank,reverse=True)
    kind,imdb,*parts=key.split(':')
    ident=imdb+(':'+':'.join(parts) if parts else '')
    response=fetch(f'{PROVIDER}/stream/{kind}/{ident}.json',timeout=15)
    candidates=[]
    for stream in response.get('streams',[]):
        infohash=stream.get('infoHash','')
        if not re.fullmatch('[a-fA-F0-9]{40}',infohash): continue
        title=stream.get('title','')+' '+stream.get('name','')
        if re.search(r'2160|4K|CAM\b|\bTS\b|HDCAM|TELESYNC|AV1|x265|HEVC|H\.265',title,re.I): continue
        if not re.search('1080|720',title): continue
        candidate=dict(stream)
        hinted_filename=stream.get('behaviorHints',{}).get('filename','')
        if Path(hinted_filename).suffix.lower() in ('.iso','.rar','.zip'): continue
        seeds=re.search(r'(?:👤|seeders?[: ]*)\s*(\d+)',title,re.I)
        # Counts are hints; even a reported zero can be stale.
        candidate['_score']=(2000 if '1080' in title else 1000)+(500 if re.search('x264|h[ .]?264',title,re.I) else 0)+(200 if 'AAC' in title.upper() else 0)+min(int(seeds[1]) if seeds else 0,3000)*3
        candidates.append(candidate)
    candidates.sort(key=rank,reverse=True)
    if key=='movie:tt1254207' and not cached('open-film-failed'): candidates.insert(0,open_film)
    if not getattr(BUDGET,'speculative',False):
        remember('source-status:'+key,{'state':'listed' if candidates else 'unavailable','checked_at':time.time()},21600)
    return remember('streams:'+key,candidates[:20],21600 if not candidates else 1800)


def choose_file(files,key,preferred=None,expected=None):
    videos=[f for f in files if Path(f.get('path','')).suffix.lower() in VIDEO and not re.search(r'\b(sample|trailer|preview)\b',f.get('path',''),re.I)]
    if key.startswith('series:'):
        _,_,season,episode=key.split(':')
        pattern=rf'(?:S0*{int(season)}[ ._-]*E0*{int(episode)}(?!\d)|(?<!\d)0*{int(season)}x0*{int(episode)}(?!\d))'
        matches=[f for f in videos if re.search(pattern,f['path'],re.I)]
        # fileIdx supplied by an episode-specific provider is valid for releases
        # whose paths omit episode naming; never guess the largest season-pack file.
        if not matches and preferred is not None:
            matches=[f for f in videos if f.get('id')==int(preferred)+1
                     and not re.search(r'(?:S\d+E\d+|\d+x\d+)',f['path'],re.I)]
        if len(matches)!=1: raise ValueError('Missing or ambiguous matching episode in torrent')
        return max(matches,key=lambda f:f.get('length',0))
    if len(videos)>1 and expected:
        matching=[f for f in videos if f.get('path')==expected or Path(f.get('path','')).name==Path(expected).name]
        if len(matching)==1:return matching[0]
    if not videos: raise ValueError('No playable video')
    if len(videos)>1: raise ValueError('Ambiguous multi-movie torrent without an explicit file index')
    return max(videos,key=lambda f:f.get('length',0))


def candidate_matches(candidate,key):
    filename=candidate.get('behaviorHints',{}).get('filename','')
    title=filename or candidate.get('title','').split('\n')[0]
    if key=='movie:tt1254207' and candidate.get('infoHash')=='dd8255ecdc7ca55fb0bbf81323d87062db1f6d1c':return True
    if re.search(r'\b(sample|trailer|preview)\b',title,re.I):return False
    if key.startswith('series:'):
        _,_,season,episode=key.split(':')
        return bool(re.search(rf'(?:S0*{int(season)}[ ._-]*E0*{int(episode)}(?!\d)|(?<!\d)0*{int(season)}x0*{int(episode)}(?!\d))',title,re.I)) or candidate.get('fileIdx') is not None
    with db() as conn:row=conn.execute('SELECT meta FROM items WHERE key=?',(key,)).fetchone()
    if not row:
        # Resolve the provider identity once, never guess a movie from a release.
        meta=metadata('movie',key.split(':')[1])
    else:meta=json.loads(row[0])
    def normalized(value):return re.sub(r'[^\w]+',' ',value.casefold()).strip()
    names=[meta.get(n,'') for n in ('name','original_title','originalName')]
    # Remote metadata can retain a working title after the film is renamed.
    # The local IMDb projection supplies alternate names for the same identity,
    # never fuzzy names belonging to another movie.
    with F.db() as conn:
        local=conn.execute("SELECT name,original,year FROM home_titles WHERE imdb=? AND kind='movie'",(key.split(':')[1],)).fetchone()
        alias=conn.execute('SELECT english FROM aliases WHERE imdb=?',(key.split(':')[1],)).fetchone()
    if local:names.extend(local[:2])
    if alias:names.append(alias[0])
    text=normalized(title)
    if not any(n and (' '+normalized(n)+' ') in (' '+text+' ') for n in names):return False
    year=str(meta.get('year') or meta.get('releaseInfo') or meta.get('released') or '')[:4]
    return bool(re.fullmatch(r'\d{4}',year) and re.search(r'(?<!\d)'+year+r'(?!\d)',title))


def resolve(key,deadline=None):
    local=F.local_download(key)
    if local:return local
    # Persisted sources precede discovery, including previously verified
    # alternatives. A healthy preferred source never triggers a provider search.
    def candidates():
        seen=set()
        for selected in REL.choices(key):
            if selected.get('local'):continue
            seen.add(selected['hash'])
            yield {'infoHash':selected['hash'],'torrentUrl':selected.get('torrent_url'),'_exact':selected}
        selected=cached('resolved:'+key)
        if selected and selected.get('hash') not in seen and not REL.excluded(key,selected):
            seen.add(selected['hash']);yield {'infoHash':selected['hash'],'torrentUrl':selected.get('torrent_url'),'_exact':selected}
        for candidate in stream_candidates(key):
            if candidate['infoHash'].lower() not in seen and candidate_matches(candidate,key):yield candidate
    for candidate in candidates():
        remaining(1)
        infohash=candidate['infoHash'].lower()
        if cached('source-excluded:'+key+':'+infohash):continue
        exact=candidate.get('_exact')
        trackers=['udp://tracker.opentrackr.org:1337/announce']
        trackers += [s[len('tracker:'):] for s in candidate.get('sources',[]) if s.startswith('tracker:')]
        magnet='magnet:?xt=urn:btih:'+infohash+''.join('&tr='+U.quote(t,safe='') for t in trackers)
        try:
            status={}
            work=REL.checkpoint(key)
            metadata_started=work.get('metadata_started',time.time()) if work.get('metadata_hash')==infohash else time.time()
            end=min(time.monotonic()+max(0,METADATA_SECONDS-(time.time()-metadata_started)),deadline or float('inf'))
            REL.checkpoint(key,phase='metadata',state='preparing',source_hash=infohash,metadata_hash=infohash,metadata_started=metadata_started)
            try:status=torr({'action':'get','hash':infohash},timeout=2)
            except (ClientCancelled,PlaybackBusy,Checkpoint):raise
            except Exception:pass
            if not status.get('file_stats'):
                try:status=torr({'action':'add','link':(exact or {}).get('link') or candidate.get('torrentUrl') or magnet,'save_to_db':False},timeout=min(2,remaining(2)))
                except (ClientCancelled,PlaybackBusy,Checkpoint):raise
                except Exception:pass
            while not status.get('file_stats') and time.monotonic()<end:
                remaining(1)
                try:status=torr({'action':'get','hash':infohash},timeout=min(2,remaining(2)))
                except (ClientCancelled,PlaybackBusy,Checkpoint):raise
                except Exception:pass
                if not status.get('file_stats'):time.sleep(min(2,remaining(2)))
            if not status.get('file_stats'):
                if deadline and time.monotonic()>=deadline:raise Checkpoint('Metadata checkpoint')
                raise RuntimeError('Metadata unavailable')
            if status.get('hash',infohash).lower()!=infohash:raise ValueError('Torrent identity changed')
            if exact:
                matches=[f for f in status['file_stats'] if f.get('id')==exact['index'] and f.get('path')==exact['filename'] and f.get('length')==exact['length']]
                if len(matches)!=1:raise ValueError('Exact source file changed')
                selected=exact
            else:
                f=choose_file(status['file_stats'],key,candidate.get('fileIdx'),candidate.get('behaviorHints',{}).get('filename'))
                if key.startswith('movie:') and not candidate_matches({**candidate,'behaviorHints':{'filename':f['path']}},key):raise ValueError('Movie filename does not match requested title/year')
                selected={'hash':infohash,'index':f['id'],'filename':f['path'],'length':f['length'],
                    'source_name':candidate.get('title',''),'file_count':len(status['file_stats']),'torrent_url':candidate.get('torrentUrl'),'link':candidate.get('torrentUrl') or magnet}
            if REL.excluded(key,selected):continue
            REL.bind(key,selected)
            return remember('resolved:'+key,selected,900)
        except (ClientCancelled,PlaybackBusy,Checkpoint):raise
        except Exception:
            remaining(1)
            if deadline and deadline-time.monotonic()<.5:raise Checkpoint('Source checkpoint') from None
            if getattr(BUDGET,'speculative',False):raise
            if exact:REL.exclude(key,exact)
            remember('source-excluded:'+key+':'+infohash,True,900)
            log('Trying next source for '+key)
    raise RuntimeError('No working compatible torrent source available')


def upstream(selected):
    return TORR+'/stream/'+U.quote(Path(selected['filename']).name)+'?'+U.urlencode({'link':selected['hash'],'index':selected['index'],'play':''})


def subtitle_config():
    path=Path('/run/secrets/opensubtitles')
    return json.loads(path.read_text()) if path.is_file() and path.stat().st_size else {}


def wizdom_search(key):
    """Use the series IMDb ID plus exact season/episode, never an episode IMDb ID."""
    kind,imdb,*episode=key.split(':')
    identity(kind,imdb,*episode)
    cachekey='wizdom-search:'+key
    saved=cached(cachekey)
    if saved is not None: return saved
    params={'action':'by_id','imdb':imdb,'season':episode[0] if episode else 0,
            'episode':episode[1] if episode else 0}
    result=fetch('https://wizdom.xyz/api/search?'+U.urlencode(params),timeout=4)
    if not isinstance(result,list): raise ValueError('Invalid Wizdom response')
    return remember(cachekey,[r for r in result if isinstance(r,dict) and isinstance(r.get('id'),int)],86400)


def store_subtitle(key,content,lang):
    if content.startswith((b'\xff\xfe',b'\xfe\xff')):text=content.decode('utf-16')
    else:
        try:text=content.decode('utf-8-sig')
        except UnicodeDecodeError:text=content.decode('cp1255')
    text=text.replace('\r\n','\n').replace('\r','\n')
    if not re.search(r'\d{2}:\d{2}:\d{2}[,.]\d{3}\s*-->',text):raise ValueError('Not an SRT subtitle')
    # Cue numbering and newline encoding do not make a different translation.
    canonical=re.sub(r'(?m)^\d+\s*\n(?=\d{2}:\d{2}:\d{2}[,.])','',text).strip()
    variant=hashlib.sha256(canonical.encode()).hexdigest()[:16]
    folder=DATA/'subtitles'/hashlib.sha256(key.encode()).hexdigest()[:24]
    folder.mkdir(parents=True,exist_ok=True)
    target=folder/(( 'he' if lang=='heb' else 'en')+'-'+variant+'.srt')
    if not target.is_file():
        with tempfile.NamedTemporaryFile(dir=folder,delete=False) as temporary:
            temporary.write(text.encode('utf-8'));name=temporary.name
        try:Path(name).replace(target)
        finally:Path(name).unlink(missing_ok=True)
    return {'lang':lang,'path':str(target),'variant':variant}


def wizdom_subtitles(key,filename,limit=2):
    limit=max(1,min(2,limit))
    cachekey='wizdom-subtitles-v2:'+key+':'+hashlib.sha256(filename.encode()).hexdigest()[:12]+':'+str(limit)
    saved=cached(cachekey)
    if saved is not None and all(Path(sub['path']).is_file() for sub in saved):return saved
    matches=list(wizdom_search(key))
    if not matches:return remember(cachekey,[],3600)
    normalize=lambda text: re.sub(r'[^a-z0-9]+',' ',re.sub(
        r'\.(?:mkv|mp4|avi|webm|m4v|ts|srt)$','',Path(text).name.lower())).strip()
    stem=normalize(filename)
    matches.sort(key=lambda r:SequenceMatcher(None,stem,normalize(r.get('versioname') or '')).ratio(),reverse=True)
    output=[];seen=set()
    for item in matches[:4]:
        if len(output)>=limit:break
        try:
            content=fetch('https://wizdom.xyz/api/files/sub/'+str(item['id']),raw=True,timeout=4)
            with zipfile.ZipFile(io.BytesIO(content)) as archive:
                entries=[e for e in archive.infolist() if e.filename.lower().endswith('.srt')
                         and not e.is_dir() and e.file_size<=2*1024*1024]
                if not entries:continue
                content=archive.read(entries[0])
            sub=store_subtitle(key,content,'heb')
            if sub['variant'] in seen:continue
            seen.add(sub['variant'])
            sub.update(provider='WizdomSubs',release=item.get('versioname',''),
                release_match=normalize(item.get('versioname') or '')==stem)
            output.append(sub)
        except (ClientCancelled,PlaybackBusy,Checkpoint):raise
        except (ValueError,UnicodeError,zipfile.BadZipFile,urllib.error.URLError,TimeoutError):continue
    return remember(cachekey,output,86400 if output else 3600)


def external_subtitles(key,filename,have_english=False,max_hebrew=2):
    try:
        hebrew=wizdom_subtitles(key,filename,limit=max_hebrew)
        if hebrew: return hebrew
    except Exception:
        log('Wizdom subtitles unavailable for '+key+'; checking fallback')
    config=subtitle_config()
    if not config.get('api_key'): return []
    cachekey='subtitles-v2:'+key+':'+hashlib.sha256(filename.encode()).hexdigest()[:12]
    saved=cached(cachekey)
    if saved is not None: return saved
    kind,imdb,*episode=key.split(':')
    headers={'Api-Key':config['api_key'],'User-Agent':'HomeMediaCatalogue v1.0'}
    params={'imdb_id':imdb[2:],'languages':'he,en','order_by':'download_count','type':'movie' if kind=='movie' else 'episode'}
    if episode: params.update(parent_imdb_id=imdb[2:],season_number=episode[0],episode_number=episode[1])
    if episode: params.pop('imdb_id',None)
    token=cached('opensubtitles-login')
    if not token and config.get('username') and config.get('password'):
        login=fetch('https://api.opensubtitles.com/api/v1/login',{'username':config['username'],'password':config['password']},headers,timeout=5)
        token=login.get('token')
        if token: remember('opensubtitles-login',token,3600)
    if token: headers['Authorization']='Bearer '+token
    output=[]; folder=DATA/'subtitles'/hashlib.sha256(key.encode()).hexdigest()[:24]
    folder.mkdir(parents=True,exist_ok=True)
    for lang in ('he','en'):
        if lang=='en' and have_english: continue
        # Query Hebrew independently so popular English results cannot crowd it
        # out of the provider's first page.
        results=fetch('https://api.opensubtitles.com/api/v1/subtitles?'+U.urlencode({**params,'languages':lang}),headers=headers,timeout=5).get('data',[])
        matches=[r['attributes'] for r in results if r['attributes'].get('language')==lang and not r['attributes'].get('foreign_parts_only')]
        if not matches: continue
        # Prefer a release-name match; exact feature identity is enforced by IDs.
        stem=Path(filename).stem.lower()
        matches.sort(key=lambda a:(str(a.get('release','')).lower() in stem if a.get('release') else False, a.get('download_count',0)),reverse=True)
        item=matches[0]
        link=fetch('https://api.opensubtitles.com/api/v1/download',{'file_id':item['files'][0]['file_id'],'sub_format':'srt'},headers,timeout=5)
        content=fetch(link['link'],raw=True,timeout=5)
        sub=store_subtitle(key,content,'heb' if lang=='he' else 'eng')
        sub['release_match']=str(item.get('release','')).lower() in stem if item.get('release') else False
        output.append(sub)
        # Download only the preferred available language to respect account quotas.
        break
    return remember(cachekey,output,86400)


def run_preflight(command,**kwargs):
    cancel=getattr(BUDGET,'cancel',None)
    if cancel is None:return subprocess.run(command,**kwargs)
    timeout=kwargs.pop('timeout');end=time.monotonic()+timeout
    kwargs.pop('capture_output',None)
    with subprocess.Popen(command,stdout=subprocess.PIPE,stderr=subprocess.PIPE,**kwargs) as process:
        try:
            while True:
                remaining(1)
                left=end-time.monotonic()
                if left<=0:raise subprocess.TimeoutExpired(command,timeout)
                try:
                    out,err=process.communicate(timeout=min(.25,left))
                    return subprocess.CompletedProcess(command,process.returncode,out,err)
                except subprocess.TimeoutExpired:continue
        except BaseException:
            process.kill();process.communicate();raise


def probe(selected,key,deadline=None):
    token=hashlib.sha256(os.urandom(32)).hexdigest()[:32]
    raw_url=selected.get('local') or REL.source_url('raw',key,selected,token)
    result=REL.run(['ffprobe','-v','error','-threads','1','-analyzeduration','4000000','-probesize','4000000',
        '-show_streams','-show_format','-of','json',raw_url],key,'video probe',selected,token,deadline or time.monotonic()+WORK_SECONDS)
    if result.stderr:raise RuntimeError('Source probe decoding error')
    return json.loads(result.stdout)


def cached_poster(folder,query):
    try:
        if query.get('style',[''])[0]!=POSTER_STYLE:return None
        if (folder/'badge-style.txt').read_text()!=POSTER_STYLE:return None
        if query.get('badge',[''])[0]!=(folder/'badge.txt').read_text():return None
        missing=folder/'poster-unavailable-at'
        if missing.exists() and time.time()-missing.stat().st_mtime>=6*3600:return None
        return (folder/'poster.jpg').read_bytes()
    except OSError:return None


def frame_rate(value):
    try:
        parts=str(value).split('/')
        return float(parts[0])/float(parts[1]) if len(parts)==2 else float(parts[0])
    except (ValueError,ZeroDivisionError,TypeError):return None


def conversion_preflight(key,selected,info,audio_index,deadline):
    """Verify a small video-copy/AAC-LC output; never saves a full movie."""
    cachekey='audio-convert-v2:'+selected['hash']+':'+str(audio_index)+':'+str(selected.get('length',0))
    if cached(cachekey):return
    folder=DATA/'audio-preflight';folder.mkdir(parents=True,exist_ok=True)
    source=selected.get('local') or REL.source_url('raw',key,selected)
    def available(maximum):return min(remaining(maximum),max(.001,deadline-time.monotonic()))
    with tempfile.TemporaryDirectory(dir=folder,prefix='sample-') as directory:
        output=Path(directory)/'sample.ts'
        command=['ffmpeg','-v','error','-threads','1','-rw_timeout','6000000','-i',source,'-t','3',
            '-map','0:v:0','-map',f'0:{audio_index}','-c:v','copy','-c:a','aac','-profile:a','aac_low',
            '-b:a','192k','-ac','2','-ar','48000','-sn','-f','mpegts',str(output)]
        converted=run_preflight(command,capture_output=True,timeout=available(15))
        if converted.returncode:raise RuntimeError('Audio conversion failed')
        result=run_preflight(['ffprobe','-v','error','-show_streams','-of','json',str(output)],capture_output=True,timeout=available(5))
        if result.returncode:raise RuntimeError('Converted sample probe failed')
        streams=json.loads(result.stdout).get('streams',[])
        original=next((s for s in info.get('streams',[]) if s.get('codec_type')=='video'),{})
        if not any(s.get('codec_type')=='video' and s.get('codec_name')==original.get('codec_name') for s in streams):
            raise RuntimeError('Converted sample video missing')
        if not any(s.get('codec_type')=='audio' and s.get('codec_name')=='aac' and s.get('profile')=='LC' for s in streams):
            raise RuntimeError('Converted sample is not AAC-LC')
        decoded=run_preflight(['ffmpeg','-v','error','-threads','1','-i',str(output),'-t','1','-f','null','-'],capture_output=True,timeout=available(8))
        if decoded.returncode:raise RuntimeError('Converted sample decode failed')
    remember(cachekey,{'video_copy':True,'audio':'aac','profile':'LC','checked_at':time.time()},7*86400)


def detail_playback_busy():
    """Fail closed: even paused/native/downloaded playback protects the server."""
    with LIVE_LOCK:
        if LIVE['active']>0:return True
    try:
        # This callback itself runs inside remaining(). Its status request must
        # not call the same cancellation callback recursively through fetch().
        end=min(time.monotonic()+1,getattr(BUDGET,'deadline',None) or float('inf'))
        with request_budget(end,None):
            return any(session.get('NowPlayingItem') for session in jf('/Sessions',timeout=1))
    except Exception:return True


def warm_details(key):
    # No queue: only the currently opened playable title may occupy the slot.
    if not DETAIL_LIMIT.acquire(blocking=False):return False
    with DETAIL_LOCK:
        now=time.monotonic()
        if DETAIL_RECENT.get(key,0)>now-60:
            DETAIL_LIMIT.release();return False
        if len(DETAIL_RECENT)>=256:DETAIL_RECENT.clear()
        DETAIL_RECENT[key]=now
    def work():
        started=time.monotonic()
        previous=getattr(BUDGET,'speculative',False)
        BUDGET.speculative=True
        try:
            if detail_playback_busy():return
            # Navigation already makes queue workers yield. Session checks also
            # cancel this job if playback begins while preparation is running.
            state={'next':0,'busy':False}
            def cancel():
                now=time.monotonic()
                if now>=state['next']:
                    state['busy']=detail_playback_busy();state['next']=now+1
                return state['busy']
            prepared(key,cancel=cancel)
            playback_event(key,'details_warm',started,'ok')
        except (ClientCancelled,PlaybackBusy) as error:
            playback_event(key,'details_warm',started,'cancelled',error)
        except Exception as error:
            playback_event(key,'details_warm',started,'failed',error)
        finally:
            BUDGET.speculative=previous
            DETAIL_LIMIT.release()
    try:threading.Thread(target=work,daemon=True).start()
    except Exception:
        DETAIL_LIMIT.release();raise
    return True


@contextmanager
def validation_attempt(background):
    previous=(getattr(BUDGET,'validation_started',None),getattr(BUDGET,'validation_background',False))
    BUDGET.validation_started=time.time();BUDGET.validation_background=background
    try:yield
    finally:BUDGET.validation_started,BUDGET.validation_background=previous


def prepared(key,background=False,audio_index=None,cancel=None):
    if background and F.verification_held(key):raise Checkpoint('Title temporarily protected from background verification')
    speculative=getattr(BUDGET,'speculative',False)
    deadline=time.monotonic()+(DETAIL_SECONDS if speculative else WORK_SECONDS if background else PREPARATION_SECONDS)
    if background:deadline=min(deadline,getattr(BUDGET,'worker_deadline',deadline))
    F.consume_rechecks()
    with validation_attempt(background),request_budget(deadline,cancel),playback_phase(key,'preparation'):
        with LOCKS_LOCK:lock=LOCKS.setdefault(key,threading.Lock())
        # Speculative navigation never waits behind actual Play or a worker.
        if speculative:
            remaining(1)
            if not lock.acquire(blocking=False):raise PlaybackBusy('Preparation already in progress')
        else:
            while not lock.acquire(timeout=min(.1,remaining(.1))):
                if deadline-time.monotonic()<.1:raise PlaybackBusy('Preparation already in progress')
        try:
            with REL.lock(key):return prepare_locked(key,background,audio_index,deadline)
        except MetadataPending:
            # Preserve the scheduling signal through the real preparation
            # wrapper; it must reach the worker's continue-with-other-title path.
            raise
        except (Checkpoint,TimeoutError,subprocess.TimeoutExpired,ClientCancelled) as error:
            if not speculative:
                F.enqueue(key,200)
                REL.checkpoint(key,state='checkpoint',message='Preparation continues in background; retry Play later',audio_index=audio_index)
            if isinstance(error,ClientCancelled) or (cancel and cancel()):raise ClientCancelled('Client disconnected') from None
            raise Checkpoint('Preparation continues in background') from None
        finally:lock.release()


def prepare_locked(key,background,audio_index,deadline):
    prepared_key='prepared:'+key+(':'+str(audio_index) if audio_index is not None else '')
    with LIVE_LOCK:
        if LIVE['key'] not in (None,key) and LIVE['active']>0:
            raise PlaybackBusy('Another title is currently streaming; this server supports one stream')
    saved=cached(prepared_key)
    if not background and saved and saved.get('verification_version')==VERSION and saved.get('audio_policy')==2 and saved.get('subtitle_policy')==2 and F.source_state(key) in ('verified','downloaded'):
        playback_event(key,'prepared_cache',time.monotonic(),'hit')
        return saved
    verified=cached('verified-'+prepared_key) if not background else None
    if verified and (verified.get('proof',{}).get('version')!=VERSION or F.source_state(key) not in ('verified','downloaded')
                     or REL.excluded(key,verified['selected']) or bool(F.local_download(key))!=bool(verified['selected'].get('local'))):verified=None
    for attempt in range(3):
        try:
            with playback_phase(key,'torrent_resolution'):selected=verified['selected'] if verified else resolve(key,deadline=deadline)
        except (ClientCancelled,PlaybackBusy,Checkpoint,TimeoutError,subprocess.TimeoutExpired):raise
        except Exception as error:
            cancel=getattr(BUDGET,'cancel',None)
            if cancel and cancel():raise ClientCancelled('Client disconnected') from None
            if getattr(BUDGET,'speculative',False):raise
            remaining(1)
            remember('source-status:'+key,{'state':'unavailable','checked_at':time.time()},900)
            F.validation_result(key,False,reason='Source preparation failed ('+type(error).__name__+')')
            raise
        try:
            with playback_phase(key,'video_probe'):info=verified['info'] if verified else probe(selected,key,deadline=deadline)
            video=next((s for s in info.get('streams',[]) if s.get('codec_type')=='video' and not s.get('disposition',{}).get('attached_pic')),{})
            if video.get('codec_name')=='h264' and video.get('pix_fmt') not in ('yuv420p','yuvj420p'):
                raise RuntimeError('Unsupported H264 pixel format')
            if video.get('codec_name') not in ('h264','mpeg4','vp8','vp9'):
                raise RuntimeError('Source requires unsupported video decoding')
            audio=[t for t in info.get('streams',[]) if t.get('codec_type')=='audio']
            ordered=sorted(audio,key=lambda t:0 if t.get('codec_name')=='aac' and t.get('profile')=='LC' else 1)
            selected_audio=next((t for t in ordered if t['index']==audio_index),None) if audio_index is not None else next((t for lang in ('eng','en','heb','he') for t in ordered if t.get('tags',{}).get('language')==lang),ordered[0] if ordered else None)
            if audio_index is not None and selected_audio is None:raise ValueError('Audio track not found')
            needs_conversion=selected_audio is not None and (selected_audio.get('codec_name')!='aac' or selected_audio.get('profile')!='LC')
            if needs_conversion and float(info.get('format',{}).get('duration') or 0)<=0:raise RuntimeError('Converted VOD requires known duration')
            proof=verified['proof'] if verified else REL.verify(key,selected,info,selected_audio['index'] if selected_audio else None,needs_conversion,deadline)
            break
        except (ClientCancelled,PlaybackBusy,Checkpoint,TimeoutError,subprocess.TimeoutExpired):raise
        except Exception as error:
            cancel=getattr(BUDGET,'cancel',None)
            if cancel and cancel():raise ClientCancelled('Client disconnected') from None
            if getattr(BUDGET,'speculative',False):raise
            remaining(1)
            playback_event(key,'source_rejected',time.monotonic(),'failed',error)
            verified=None
            REL.exclude(key,selected)
            remember('source-excluded:'+key+':'+selected['hash'],True,900)
            with db() as conn:conn.execute('DELETE FROM cache WHERE key=?',('resolved:'+key,))
            # Retain torrents/readers: another request may still be playing an
            # older bound source. Only this preparation advances to a fallback.
            if attempt==2:
                remember('source-status:'+key,{'state':'unavailable','checked_at':time.time()},900)
                F.validation_result(key,False,reason='Video preflight failed ('+type(error).__name__+')')
                raise
    tracks=[]; languages=set()
    for stream in info.get('streams',[]):
        typ=stream.get('codec_type')
        if typ not in ('video','audio','subtitle'): continue
        if typ=='video' and stream.get('disposition',{}).get('attached_pic'): continue
        lang=stream.get('tags',{}).get('language','und')
        lang={'he':'heb','iw':'heb','en':'eng'}.get(lang,lang)
        if typ=='subtitle': languages.add(lang)
        tracks.append({'Index':stream['index'],'Type':typ.title(),'Codec':stream.get('codec_name'),'Profile':stream.get('profile'),
            'Language':lang,'Channels':stream.get('channels'),
            'AverageFrameRate':frame_rate(stream.get('avg_frame_rate')),'RealFrameRate':frame_rate(stream.get('r_frame_rate')),
            'BitRate':int(stream['bit_rate']) if str(stream.get('bit_rate','')).isdigit() else None,
            'SampleRate':int(stream['sample_rate']) if str(stream.get('sample_rate','')).isdigit() else None,
            'PixelFormat':stream.get('pix_fmt'),'Level':stream.get('level'), 'Width':stream.get('width'),
            'Height':stream.get('height'),'IsExternal':False,
            'IsDefault':bool(stream.get('disposition',{}).get('default')),
            'IsForced':bool(stream.get('disposition',{}).get('forced'))})
    # Keep actual indexes. Native HLS converts a selected non-LC track
    # while copying video; direct AAC-LC retains the original stream.
    tracks.sort(key=lambda t:(0 if t['Type']=='Video' else 1 if t['Type']=='Audio' else 2,0 if t.get('Codec')=='aac' and t.get('Profile')=='LC' else 1))
    if not any(t['Type']=='Video' for t in tracks): raise RuntimeError('No video in selected source')
    # Show at most two Hebrew choices, including any embedded track. Retain
    # original stream indexes and all other languages; explicit None stays -1.
    hebrew=[t for t in tracks if t['Type']=='Subtitle' and t['Language']=='heb']
    keep={t['Index'] for t in hebrew[:2]}
    next_index=max(t['Index'] for t in tracks)+1
    tracks=[t for t in tracks if t['Type']!='Subtitle' or t['Language']!='heb' or t['Index'] in keep]
    if not background and len(hebrew)<2:
        pool=ThreadPoolExecutor(max_workers=1)
        cancel=getattr(BUDGET,'cancel',None)
        def subtitles():
            with request_budget(deadline,cancel):
                return external_subtitles(key,selected['filename'],'eng' in languages,max_hebrew=2-len(hebrew))
        try:
            with playback_phase(key,'subtitle_lookup'):
                external=pool.submit(subtitles).result(timeout=max(.001,min(15,deadline-time.monotonic())))
            he_count=len(hebrew)
            for sub in external:
                if sub['lang']=='heb' and he_count>=2:continue
                if sub['lang']=='heb':he_count+=1
                variant=sub.get('variant')
                tracks.append({'Index':next_index,'Type':'Subtitle','Codec':'srt',
                    'Language':sub['lang'],'IsExternal':True,'SupportsExternalStream':True,
                    'DeliveryMethod':'External','DeliveryUrl':signed_path('sub',key)+'&lang='+sub['lang']+('&variant='+variant if variant else ''),
                    'Title':sub['lang'].upper()+' (downloaded)'})
                next_index+=1;languages.add(sub['lang'])
        except Exception: log('Subtitle download unavailable or timed out for '+key)
        finally:pool.shutdown(wait=False,cancel_futures=True)
    for number,track in enumerate((t for t in tracks if t['Type']=='Subtitle' and t['Language']=='heb'),1):
        track['Title']='עברית '+str(number)
    preferred=next((t['Index'] for lang in ('heb','eng') for t in tracks if t['Type']=='Subtitle' and t['Language']==lang),None)
    output={'selected':selected,'MediaStreams':tracks,'DefaultSubtitleStreamIndex':preferred,
        'Container':Path(selected['filename']).suffix[1:].lower(),
        'RunTimeTicks':int(float(info.get('format',{}).get('duration',0))*10000000),
        'Bitrate':int(info.get('format',{}).get('bit_rate',5000000)),
        'languages':sorted(languages),'checked_at':proof.get('checked_at',time.time()),'audio_policy':2,'subtitle_policy':2,
        'verification_version':VERSION,'PlaybackUrl':REL.source_url('play',key,selected),
        'DefaultAudioStreamIndex':selected_audio['index'] if selected_audio else None,'AudioConversionRequired':needs_conversion}
    if getattr(BUDGET,'speculative',False):remaining(1)
    with db() as conn:
        conn.execute('INSERT OR REPLACE INTO playback VALUES (?,?,?)',(key,json.dumps(output),time.time()))
    accepted=True if verified else F.validation_result(key,True,proof=proof)
    if accepted is not False:
        REL.success(key,selected)
        remember('verified-'+prepared_key,{'selected':selected,'info':info,'proof':proof},max(1,300-(time.time()-proof.get('checked_at',time.time()))))
    return output if background else remember(prepared_key,output,max(1,300-(time.time()-output['checked_at'])))


def collection_items(**filters):
    """Read native items in small pages without losing filtered-out page slots."""
    offset=0
    ceiling=1
    size=100
    while offset<ceiling:
        query={**filters,'StartIndex':offset,'Limit':size,'EnableUserData':'false',
               'EnableTotalRecordCount':'true' if offset==0 else 'false'}
        page=jf('/Items?'+U.urlencode(query),timeout=60)
        # The availability result filter subtracts hidden entries from this
        # page's total. At most `size` entries were removed, so this bound
        # visits every native slot, including pages containing no ready titles.
        ceiling=max(ceiling,int(page.get('TotalRecordCount',0))+size)
        yield from page.get('Items',[])
        offset+=size


def update_collections():
    """Create native Jellyfin collections after its library scan has indexed items."""
    with db() as conn:
        rows=conn.execute('SELECT kind,imdb,buckets FROM items').fetchall()
    membership={(kind,imdb):json.loads(names) for kind,imdb,names in rows}
    buckets={}
    for item in collection_items(Recursive='true',IncludeItemTypes='Movie,Series',Fields='Path,ProviderIds'):
        if not item.get('Path','').startswith('/catalogue/'): continue
        imdb=item.get('ProviderIds',{}).get('Imdb')
        kind='movie' if item['Type']=='Movie' else 'series'
        for bucket in membership.get((kind,imdb),[]):
            if bucket in ('Recent Releases','Popular'):
                buckets.setdefault('Online '+bucket,[]).append(item['Id'])
    existing=list(collection_items(Recursive='true',IncludeItemTypes='BoxSet'))
    for name,ids in buckets.items():
        collection=next((i for i in existing if i['Name']==name),None)
        if not collection:
            collection=jf('/Collections?'+U.urlencode({'name':name}),{},method='POST')
        current=collection_items(ParentId=collection['Id'])
        present={i['Id'] for i in current}
        pending=[item_id for item_id in ids if item_id not in present]
        for start in range(0,len(pending),50):
            jf('/Collections/'+collection['Id']+'/Items?'+U.urlencode({'ids':','.join(pending[start:start+50])}),{},method='POST')
    log('Native catalogue collections updated')


class Handler(BaseHTTPRequestHandler):
    protocol_version='HTTP/1.1'
    def log_message(self,*args): pass

    def do_POST(self):
        parsed=U.urlsplit(self.path);parts=parsed.path.strip('/').split('/')
        if len(parts)!=2 or parts[0]!='download':return self.reply({'error':'Not found'},404)
        try:
            key=U.unquote(parts[1]);identity(*key.split(':'))
            sig=U.parse_qs(parsed.query).get('sig',[''])[0]
            if not hmac.compare_digest(sig,signature(key)):return self.reply({'error':'Unauthorized'},401)
            length=int(self.headers.get('Content-Length','0'))
            if length>1024:return self.reply({'error':'Request too large'},413)
            if length:self.rfile.read(length)
            info=prepared(key)
            if info['selected'].get('local'):return self.reply({'state':'ready'})
            record=F.queue_download(key,info['selected'])
            return self.reply({'state':record['state']})
        except Exception:
            log('Download request deferred');return self.reply({'error':'Download could not be queued'},503)

    def local_video(self,selected):
        path=Path(selected['local']);size=path.stat().st_size
        start,end=0,size-1;partial=False
        header=self.headers.get('Range')
        if header:
            match=re.fullmatch(r'bytes=(\d*)-(\d*)',header)
            if not match or not any(match.groups()):
                self.send_response(416);self.send_header('Content-Range',f'bytes */{size}');self.send_header('Content-Length','0');self.end_headers();return
            first,last=match.groups()
            if first:start=int(first);end=min(size-1,int(last)) if last else size-1
            else:start=max(0,size-int(last))
            if start>=size or end<start:
                self.send_response(416);self.send_header('Content-Range',f'bytes */{size}');self.send_header('Content-Length','0');self.end_headers();return
            partial=True
        self.send_response(206 if partial else 200)
        self.send_header('Content-Type',mimetypes.guess_type(path.name)[0] or 'application/octet-stream')
        self.send_header('Content-Length',str(end-start+1));self.send_header('Accept-Ranges','bytes')
        if partial:self.send_header('Content-Range',f'bytes {start}-{end}/{size}')
        self.end_headers()
        if self.command!='HEAD':
            with path.open('rb') as stream:
                stream.seek(start);remaining=end-start+1
                while remaining:
                    chunk=stream.read(min(128*1024,remaining))
                    if not chunk:break
                    self.wfile.write(chunk);remaining-=len(chunk)

    def reply(self,value,status=200,ctype='application/json'):
        content=json.dumps(value).encode() if ctype=='application/json' else value
        self.send_response(status)
        self.send_header('Content-Type',ctype)
        self.send_header('Content-Length',str(len(content)))
        self.end_headers()
        if self.command!='HEAD': self.wfile.write(content)

    def poster_reply(self,content):
        etag='"'+hashlib.sha256(content).hexdigest()+'"'
        matched=any(tag.strip().removeprefix('W/') in (etag,'*') for tag in self.headers.get('If-None-Match','').split(','))
        self.send_response(304 if matched else 200)
        self.send_header('ETag',etag)
        self.send_header('Cache-Control','private, max-age=60')
        if not matched:
            self.send_header('Content-Type','image/jpeg')
            self.send_header('Content-Length',str(len(content)))
        self.end_headers()
        if not matched and self.command!='HEAD':self.wfile.write(content)

    def disconnected(self):
        try:
            if not select.select([self.connection],[],[],0)[0]:return False
            return self.connection.recv(1,socket.MSG_PEEK|socket.MSG_DONTWAIT)==b''
        except (BlockingIOError,InterruptedError):return False
        except OSError:return True

    def do_HEAD(self): self.do_GET()

    def do_GET(self):
        counted_play=False
        response_started=False
        stream_started=time.monotonic()
        key=None;parts=[]
        try:
            parsed=U.urlsplit(self.path); parts=parsed.path.strip('/').split('/')
            if parsed.path=='/ready':
                ready=F.ready_keys()
                ready.update(":".join(key.split(":")[:2]) for key in list(ready))
                wanted=U.parse_qs(parsed.query).get('keys',[''])[0]
                if wanted:ready.intersection_update(wanted.split(','))
                return self.reply({'titles':sorted(ready)})
            if parsed.path=='/series-counts':
                wanted=U.parse_qs(parsed.query).get('ids',[''])[0].split(',')[:1000]
                return self.reply(F.series_episode_counts(wanted))
            if parsed.path=='/validation':
                with F.db() as conn:
                    states=dict(conn.execute('SELECT state,COUNT(*) FROM validation GROUP BY state'))
                    due=conn.execute("SELECT COUNT(*) FROM validation WHERE state!='verified' AND next_check<=?",(time.time(),)).fetchone()[0]
                return self.reply({'states':states,'due':due})
            if parsed.path=='/health':
                with db() as conn: count=conn.execute('SELECT kind,COUNT(*) FROM items GROUP BY kind').fetchall()
                return self.reply({'status':'ok','catalogue':dict(count),'subtitles_configured':True,
                    'subtitle_providers':['WizdomSubs']+(['OpenSubtitles'] if subtitle_config().get('api_key') else [])})
            if parts[0]=='poster' and len(parts)==3:
                kind,imdb=parts[1],parts[2].removesuffix('.jpg')
                if kind not in ('movie','series') or not re.fullmatch(r'tt\d{5,12}',imdb):
                    return self.reply({'error':'Not found'},404)
                folder=LIBRARY/('movies' if kind=='movie' else 'shows')/imdb
                query=U.parse_qs(parsed.query)
                content=cached_poster(folder,query)
                if content is not None:return self.poster_reply(content)
                folder.mkdir(parents=True,exist_ok=True)
                with POSTER_LIMIT, POSTER_LOCKS[int(imdb[2:])%len(POSTER_LOCKS)]:
                    with F.db() as conn:
                        row=conn.execute('SELECT meta FROM items WHERE key=?',(kind+':'+imdb,)).fetchone()
                        if not row:row=conn.execute('SELECT meta FROM discovery WHERE kind=? AND imdb=?',(kind,imdb)).fetchone()
                    meta=json.loads(row[0]) if row else {'poster':f'https://images.metahub.space/poster/medium/{imdb}/img'}
                    if not str(meta.get('poster','')).startswith('https://'):
                        meta['poster']=f'https://images.metahub.space/poster/medium/{imdb}/img'
                    make_poster(folder,meta,kind,imdb)
                    if not (folder/'poster.jpg').exists():return self.reply({'error':'Artwork unavailable'},404)
                    content=(folder/'poster.jpg').read_bytes()
                return self.poster_reply(content)
            # Stremio metadata/search proxy, used by the Jellyfin plugin.
            if parts[0]=='label' and len(parts)==2:
                imdb=parts[1]
                if not re.fullmatch(r'tt\d{5,12}',imdb):return self.reply({'error':'Invalid title'},400)
                with F.db() as conn:row=conn.execute('SELECT hebrew,english FROM aliases WHERE imdb=?',(imdb,)).fetchone()
                kind=U.parse_qs(parsed.query).get('kind',['movie'])[0]
                with F.db() as conn:details=conn.execute('SELECT meta FROM items WHERE key=?',(kind+':'+imdb,)).fetchone()
                urls=trailer_urls(json.loads(details[0])) if details and kind in ('movie','series') else []
                return self.reply({'hebrew':row[0] if row else None,'english':row[1] if row else None,
                    'trailers':[{'Url':url} for url in urls], 'poster':F.poster_url(kind,imdb) if kind in ('movie','series') else None})
            if parts[0]=='browse' and len(parts)==2:
                query=U.parse_qs(parsed.query)
                started=time.monotonic()
                log('Catalogue browse started')
                page=F.browse(parts[1],query.get('offset',[0])[0],query.get('limit',[100])[0],query.get('year',[None])[0],
                    query.get('sort',['PremiereDate'])[0],query.get('order',['Descending'])[0],query.get('month',[None])[0])
                log(f'Catalogue browse completed in {time.monotonic()-started:.2f}s')
                return self.reply(page)
            if parts[0] in ('manifest.json','catalog','meta'):
                if parts[0]=='manifest.json':
                    return self.reply(fetch(CINEMETA+'/manifest.json'))
                search=re.search(r'/search=([^/]+)\.json$',U.unquote(parsed.path)) if parts[0]=='catalog' else None
                if search and len(parts)>=3:
                    return self.reply({'metas':F.search(parts[1],search.group(1))})
                url=CINEMETA+parsed.path+('?' + parsed.query if parsed.query else '')
                cachekey='catalog-api:'+parsed.path
                value=cached(cachekey)
                if value is None:
                    if parts[0]=='meta' and len(parts)>=3:
                        value={'meta':metadata(parts[1],parts[2].removesuffix('.json'))}
                    else:value=fetch(url)
                    remember(cachekey,value,900)
                value=json.loads(json.dumps(value))
                if parts[0]=='meta' and len(parts)>=3:
                    kind,imdb=parts[1],parts[2].removesuffix('.json')
                    if kind in ('movie','series') and value.get('meta'):
                        value['meta']=F.localize(value['meta'])
                        # Opening a search result persists it without fetching any video.
                        import_item(kind,value['meta'],'Search')
                        if not cached('search-refresh'):
                            remember('search-refresh',True,30)
                            timer=threading.Timer(10,lambda: jf('/Library/Refresh',{},method='POST'))
                            timer.daemon=True
                            timer.start()
                        value['meta']['poster']=F.poster_url(kind,imdb)
                elif parts[0]=='catalog' and len(parts)>=3:
                    for item in value.get('metas',[]):
                        if re.fullmatch(r'tt\d{5,12}',item.get('id','')):
                            item['poster']=F.poster_url(parts[1],item['id'])
                return self.reply(value)
            if parts[0]=='stream' and len(parts)==3:
                kind=parts[1]; ident=parts[2].removesuffix('.json').split(':')
                key=identity(kind,*ident)
                F.enqueue(key,100)
                if F.source_state(key) not in ('verified','downloaded'):
                    return self.reply({'streams':[]})
                # Opening a playable title starts one bounded speculative job.
                # Source and subtitle work share that job rather than duplicate
                # provider requests in separate warming threads.
                if self.command=='GET':warm_details(key)
                filename='video.mkv'
                local=F.local_download(key)
                streams=[{'name':'Downloaded on server' if local else 'Stream now','title':'Downloaded on server' if local else 'Stream now (1080p / 720p)',
                          'url':signed_path('play',key),'behaviorHints':{'filename':filename}}]
                if not local:
                    streams.append({'name':'Download to server + play','title':'Download to server + play',
                        'url':signed_path('play',key)+'&save=1','behaviorHints':{'filename':filename}})
                return self.reply({'streams':streams})
            if parts[0] not in ('play','raw','info','sub') or len(parts)!=2:
                return self.reply({'error':'Not found'},404)
            key=U.unquote(parts[1]); tokens=key.split(':')
            identity(tokens[0],*tokens[1:])
            sig=U.parse_qs(parsed.query).get('sig',[''])[0]
            query=U.parse_qs(parsed.query)
            source_id=query.get('source',[''])[0]
            if not hmac.compare_digest(sig,signature(key+':'+source_id if source_id else key)): return self.reply({'error':'Unauthorized'},401)
            progress=query.get('progress',[''])[0]
            if progress and (not re.fullmatch('[a-f0-9]{32}',progress) or not hmac.compare_digest(query.get('progress_sig',[''])[0],signature(key+':'+source_id+':'+progress))):
                return self.reply({'error':'Unauthorized'},401)
            if parts[0]=='info':
                audio=U.parse_qs(parsed.query).get('audio',[None])[0]
                info=prepared(key,audio_index=int(audio) if audio is not None else None,cancel=self.disconnected)
                return self.reply(info)
            if parts[0]=='sub':
                lang=U.parse_qs(parsed.query).get('lang',[''])[0]
                if lang not in ('heb','eng'): return self.reply({'error':'Not found'},404)
                variant=U.parse_qs(parsed.query).get('variant',[''])[0]
                if variant and not re.fullmatch('[a-f0-9]{16}',variant):return self.reply({'error':'Invalid subtitle'},400)
                name=('he' if lang=='heb' else 'en')+('-'+variant if variant else '')+'.srt'
                target=DATA/'subtitles'/hashlib.sha256(key.encode()).hexdigest()[:24]/name
                if not target.is_file(): return self.reply({'error':'Subtitle unavailable'},404)
                return self.reply(target.read_bytes(),ctype='application/x-subrip; charset=utf-8')
            # Initial source resolution and first data share one 15s budget.
            data_deadline=time.monotonic()+FIRST_DATA_SECONDS
            if source_id:selected=REL.lookup(key,source_id)
            elif parts[0]=='raw':return self.reply({'error':'Exact source required'},409)
            else:
                selected=prepared(key,cancel=self.disconnected)['selected']
                data_deadline=time.monotonic()+FIRST_DATA_SECONDS
            # Internal sample requests may wait through a useful slow source.
            # Preparation owns the 45-second stall and eight-minute work clocks.
            if progress:data_deadline=time.monotonic()+WORK_SECONDS
            if parts[0]=='play':
                with LIVE_LOCK:
                    if LIVE['key'] not in (None,key) and LIVE['active']>0:
                        return self.reply({'error':'A different title is already streaming; stop it before starting another.'},409)
                    LIVE.update(key=key,touched=time.monotonic())
                    LIVE['active']+=1
                    counted_play=True
            if selected.get('local'):
                response_started=True
                return self.local_video(selected)
            headers=torr_headers()
            if self.headers.get('Range'): headers['Range']=self.headers['Range']
            upstream_request=R.Request(upstream(selected),headers=headers,method=self.command)
            with playback_phase(key,'upstream_open'):
                upstream_response=(REL.open_sample(self,upstream_request,max(.001,data_deadline-time.monotonic())) if progress else R.urlopen(upstream_request,timeout=max(.001,data_deadline-time.monotonic())))
            with upstream_response as response:
                byte_start=0
                match=re.match(r'bytes (\d+)-',response.headers.get('Content-Range',''))
                if match:byte_start=int(match[1])
                # Read an initial block BEFORE committing a successful response.
                # A header-only upstream must fail promptly instead of leaving
                # the TV waiting on an already successful empty video stream.
                initial=b''
                if self.command!='HEAD':
                    with playback_phase(key,'first_video_bytes'):
                        initial=read_bounded(response,1,data_deadline)
                        if not initial:raise RuntimeError('No initial video bytes')
                playback_event(key,'stream_ready',stream_started,'ok')
                self.send_response(response.status)
                response_started=True
                for name in ('Content-Type','Content-Length','Content-Range','Accept-Ranges','Last-Modified'):
                    if response.headers.get(name): self.send_header(name,response.headers[name])
                self.send_header('Connection','close')
                self.end_headers()
                if self.command!='HEAD':
                    transferred=len(initial)
                    if progress:REL.delivered(progress,byte_start,byte_start+transferred)
                    last_progress=time.monotonic()
                    if initial:self.wfile.write(initial)
                    expected=int(response.headers.get('Content-Length','0'))
                    while True:
                        chunk=response.read1(128*1024)
                        if not chunk:
                            if expected and transferred<expected:raise RuntimeError('Upstream video ended early')
                            break
                        transferred+=len(chunk)
                        self.wfile.write(chunk)
                        if progress and time.monotonic()-last_progress>=2:
                            REL.delivered(progress,byte_start,byte_start+transferred);last_progress=time.monotonic()
                        if parts[0]=='play':
                            with LIVE_LOCK: LIVE.update(key=key,touched=time.monotonic())
                if progress and self.command!='HEAD':REL.delivered(progress,byte_start,byte_start+transferred)
                self.close_connection=True
        except (BrokenPipeError,ConnectionResetError,ClientCancelled):
            if key:playback_event(key,'request',stream_started,'cancelled')
        except Checkpoint:
            if not response_started:self.reply({'error':'Preparing source in background; retry Play shortly.'},503)
        except PlaybackBusy:
            if not response_started:self.reply({'error':'Another playback preparation is in progress; retry shortly.'},409)
        except urllib.error.HTTPError as error:
            if parts and parts[0]=='play' and error.code!=416:F.validation_result(key,False,reason='Upstream HTTP failure')
            if error.code==416 and not response_started:
                self.send_response(416)
                if error.headers.get('Content-Range'):
                    self.send_header('Content-Range',error.headers['Content-Range'])
                self.send_header('Content-Length','0')
                self.end_headers()
            elif not response_started:
                self.reply({'error':'Source provider unavailable; retry later.'},503)
            else: self.close_connection=True
        except Exception as error:
            if key:playback_event(key,'request',stream_started,'failed',error)
            else:log('Request failed: '+type(error).__name__)
            if parts and parts[0]=='play':F.validation_result(key,False,reason='Stream read failure')
            try:
                if not response_started:
                    self.reply({'error':'Source unavailable; retry later or select another title.'},503)
                else: self.close_connection=True
            except OSError: pass
        finally:
            if counted_play:
                with LIVE_LOCK: LIVE['active']=max(0,LIVE['active']-1)


def main():
    # Catalogue directories inherit group users; Jellyfin must be able to save
    # its native metadata and downloaded sidecar subtitles beside STRM files.
    os.umask(0o002)
    parser=argparse.ArgumentParser()
    parser.add_argument('command',choices=('serve','sync','collections','badges','trailers','discovery-sync','availability','downloads','imdb-sync','metadata-import'),default='serve',nargs='?')
    parser.add_argument('--movies',type=int,default=1000)
    parser.add_argument('--shows',type=int,default=200)
    args=parser.parse_args()
    BUDGET.metadata_background=args.command!='serve'
    if args.command=='metadata-import':
        log('LAN metadata imports applied: '+str(metadata_import(seed=True)))
        return
    if args.command=='sync': return sync(args.movies,args.shows)
    if args.command=='collections': return update_collections()
    if args.command=='badges': return refresh_subtitle_badges()
    if args.command=='trailers': return refresh_trailers()
    if args.command=='discovery-sync': return F.sync_discovery()
    if args.command=='availability': return F.warm_availability()
    if args.command=='downloads': return F.manage_downloads()
    if args.command=='imdb-sync': return F.update_index()
    db().close()
    server=ThreadingHTTPServer(('0.0.0.0',8092),Handler)
    server.daemon_threads=True
    log('Catalogue bridge ready')
    server.serve_forever()


F=Features(globals())
REL=Reliability(globals())
if __name__=='__main__': main()
