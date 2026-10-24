"""LAN-only catalogue and torrent playback adapter. Secrets never enter logs."""
import argparse
import base64
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
import socket
import sqlite3
import subprocess
import threading
import tempfile
import time
import urllib.error
import urllib.parse as U
import urllib.request as R
import xml.etree.ElementTree as ET
import zipfile
from features import Features

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
POSTER_STYLE = '3'
STREAM_LIMIT = threading.Semaphore(1)
POSTER_LIMIT = threading.Semaphore(3)
POSTER_LOCKS = [threading.Lock() for _ in range(64)]
LIVE_LOCK = threading.Lock()
LIVE = {'key': None, 'touched': 0, 'active': 0}


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
    conn = sqlite3.connect(DATA / 'catalogue.sqlite', timeout=30, factory=Connection)
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
    body = json.dumps(data).encode() if data is not None else None
    request = R.Request(url, data=body, headers={'User-Agent':'HomeMediaCatalogue/1.0', 'Content-Type':'application/json', **(headers or {})}, method=method)
    with R.urlopen(request, timeout=timeout) as response:
        result = response.read(12*1024*1024)
        return result if raw else (json.loads(result) if result else None)


def jf(path, data=None, method=None):
    return fetch(JF+path,data,{'X-Emby-Token':JFKEY.read_text().strip()},method=method)


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
    if saved is None:
        try:
            saved=fetch(f'{CINEMETA}/meta/{kind}/{imdb}.json',timeout=8)['meta']
            if not isinstance(saved,dict):raise ValueError('Metadata not found')
        except Exception:
            index=DATA/'imdb/catalogue.sqlite'
            if not index.exists():raise
            conn=sqlite3.connect('file:'+str(index)+'?mode=ro',uri=True)
            try:row=conn.execute('SELECT name,year,original FROM titles WHERE kind=? AND imdb=?',(kind,imdb)).fetchone()
            finally:conn.close()
            if not row or kind=='series':raise
            saved={'id':imdb,'name':row[0],'originalName':row[2],'releaseInfo':str(row[1]),'type':kind,
                'poster':f'https://images.metahub.space/poster/medium/{imdb}/img'}
        if not isinstance(saved,dict):raise ValueError('Metadata not found')
        remember(key,saved,21600)
    F.record_dates(kind,[saved])
    return F.localize(saved)


def poster_status(kind,imdb):
    with db() as conn:
        rows=conn.execute('SELECT value FROM playback WHERE key LIKE ?', (f'{kind}:{imdb}%',)).fetchall()
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
    with db() as conn:
        cached_rows=conn.execute("SELECT key,value FROM cache WHERE expires>? AND (key LIKE 'availability:%' OR key LIKE 'wizdom-search:movie:%')",(time.time(),)).fetchall()
        playback_rows=conn.execute('SELECT key,value FROM playback WHERE updated>?',(time.time()-86400,)).fetchall()
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
                          and v.get('released','9999')[:10]<=dt.date.today().isoformat()]
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
    draw.rounded_rectangle((8,8,83,48),radius=6,fill={'HE':'#126b47','EN':'#b3261e','?':'#555555'}[badge])
    draw.text((22,12),badge,font=font,fill='white')
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
        if not cached('source-status:'+key):
            remember('source-status:'+key,{'state':'listed' if saved else 'unavailable','checked_at':time.time()},21600)
        if key=='movie:tt1254207' and not cached('open-film-failed') and not any(s['infoHash']==open_film['infoHash'] for s in saved):
            return [open_film]+saved
        return saved
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
        if seeds and int(seeds[1])==0:continue
        candidate['_score']=(2000 if '1080' in title else 1000)+(500 if re.search('x264|h[ .]?264',title,re.I) else 0)+(200 if 'AAC' in title.upper() else 0)+min(int(seeds[1]) if seeds else 0,3000)*3
        candidates.append(candidate)
    candidates.sort(key=lambda x:x['_score'],reverse=True)
    if key=='movie:tt1254207' and not cached('open-film-failed'): candidates.insert(0,open_film)
    remember('source-status:'+key,{'state':'listed' if candidates else 'unavailable','checked_at':time.time()},21600)
    return remember('streams:'+key,candidates[:5],21600 if not candidates else 1800)


def choose_file(files,key,preferred=None):
    videos=[f for f in files if Path(f.get('path','')).suffix.lower() in VIDEO and not re.search(r'\bsample\b',f.get('path',''),re.I)]
    if key.startswith('series:'):
        _,_,season,episode=key.split(':')
        pattern=rf'(?:S0*{int(season)}[ ._-]*E0*{int(episode)}(?!\d)|(?<!\d)0*{int(season)}x0*{int(episode)}(?!\d))'
        matches=[f for f in videos if re.search(pattern,f['path'],re.I)]
        # fileIdx supplied by an episode-specific provider is valid for releases
        # whose paths omit episode naming; never guess the largest season-pack file.
        if not matches and preferred is not None:
            matches=[f for f in videos if f.get('id')==int(preferred)+1
                     and not re.search(r'(?:S\d+E\d+|\d+x\d+)',f['path'],re.I)]
        if not matches: raise ValueError('No matching episode in torrent')
        return max(matches,key=lambda f:f.get('length',0))
    if preferred is not None:
        matching=[f for f in videos if f.get('id')==int(preferred)+1]
        if matching: return matching[0]
    if not videos: raise ValueError('No playable video')
    if len(videos)>1: raise ValueError('Ambiguous multi-movie torrent without an explicit file index')
    return max(videos,key=lambda f:f.get('length',0))


def resolve(key,deadline=None):
    local=F.local_download(key)
    if local:return local
    selected=cached('resolved:'+key)
    if selected:
        try:
            status=torr({'action':'get','hash':selected['hash']},timeout=3)
            if status.get('file_stats'): return selected
        except Exception: pass
    for candidate in stream_candidates(key):
        if deadline is not None and time.monotonic()>=deadline:break
        infohash=candidate['infoHash'].lower()
        trackers=['udp://tracker.opentrackr.org:1337/announce']
        trackers += [s[len('tracker:'):] for s in candidate.get('sources',[]) if s.startswith('tracker:')]
        magnet='magnet:?xt=urn:btih:'+infohash+''.join('&tr='+U.quote(t,safe='') for t in trackers)
        try:
            status={}
            # Add waits for magnet metadata on TorrServer. A short socket
            # timeout does not mean the background metadata lookup failed.
            # Leave time for probing within the overall preparation budget.
            end=min(time.monotonic()+27,deadline-5 if deadline is not None else float('inf'))
            try:
                status=torr({'action':'get','hash':infohash},timeout=2)
            except Exception:pass
            if not status.get('file_stats'):
                try:
                    status=torr({'action':'add','link':candidate.get('torrentUrl') or magnet,'save_to_db':False},
                        timeout=max(.1,min(20,end-time.monotonic())))
                except (TimeoutError,urllib.error.URLError):
                    # The API request can outlive our socket; poll for its
                    # completed torrent instead of dropping it immediately.
                    pass
            while not status.get('file_stats') and time.monotonic()<end:
                try:status=torr({'action':'get','hash':infohash},timeout=max(.1,min(2,end-time.monotonic())))
                except Exception:pass
                if not status.get('file_stats'):time.sleep(.5)
            selected_file=choose_file(status.get('file_stats',[]),key,candidate.get('fileIdx'))
            return remember('resolved:'+key,{'hash':infohash,'index':selected_file['id'],
                'filename':selected_file['path'],'length':selected_file['length'],
                'source_name':candidate.get('title',''),'file_count':len(status.get('file_stats',[])),
                'torrent_url':candidate.get('torrentUrl')},900)
        except Exception:
            log('Trying next source for '+key)
            try: torr({'action':'drop','hash':infohash},timeout=2)
            except Exception: pass
    raise RuntimeError('No working 1080p/720p torrent source available')


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


def wizdom_subtitles(key,filename):
    cachekey='wizdom-subtitles:'+key+':'+hashlib.sha256(filename.encode()).hexdigest()[:12]
    saved=cached(cachekey)
    if saved is not None and all(Path(s['path']).is_file() for s in saved): return saved
    matches=wizdom_search(key)
    if not matches: return remember(cachekey,[],3600)
    normalize=lambda text: re.sub(r'[^a-z0-9]+',' ',re.sub(
        r'\.(?:mkv|mp4|avi|webm|m4v|ts|srt)$','',Path(text).name.lower())).strip()
    stem=normalize(filename)
    matches.sort(key=lambda r:SequenceMatcher(None,stem,normalize(r.get('versioname') or '')).ratio(),reverse=True)
    for item in matches[:2]:
        try:
            content=fetch('https://wizdom.xyz/api/files/sub/'+str(item['id']),raw=True,timeout=4)
            with zipfile.ZipFile(io.BytesIO(content)) as archive:
                entries=[e for e in archive.infolist() if e.filename.lower().endswith('.srt')
                         and not e.is_dir() and e.file_size<=2*1024*1024]
                if not entries: continue
                # Read bytes only: never extract provider-controlled archive paths.
                content=archive.read(entries[0])
            if content.startswith((b'\xff\xfe',b'\xfe\xff')): text=content.decode('utf-16')
            else:
                try: text=content.decode('utf-8-sig')
                except UnicodeDecodeError: text=content.decode('cp1255')
            if not re.search(r'\d{2}:\d{2}:\d{2}[,.]\d{3}\s*-->',text):
                raise ValueError('Not an SRT subtitle')
            folder=DATA/'subtitles'/hashlib.sha256(key.encode()).hexdigest()[:24]
            folder.mkdir(parents=True,exist_ok=True)
            target=folder/'he.srt'
            temporary=folder/'he.srt.tmp'
            temporary.write_text(text,encoding='utf-8')
            temporary.replace(target)
            return remember(cachekey,[{'lang':'heb','path':str(target),'provider':'WizdomSubs',
                'release':item.get('versioname',''),'release_match':normalize(item.get('versioname') or '')==stem}],86400)
        except (ValueError,UnicodeError,zipfile.BadZipFile): continue
    return remember(cachekey,[],3600)


def external_subtitles(key,filename,have_english=False):
    try:
        hebrew=wizdom_subtitles(key,filename)
        if hebrew: return hebrew
    except Exception:
        log('Wizdom subtitles unavailable for '+key+'; checking fallback')
    config=subtitle_config()
    if not config.get('api_key'): return []
    cachekey='subtitles:'+key+':'+hashlib.sha256(filename.encode()).hexdigest()[:12]
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
        target=folder/(lang+'.srt')
        target.write_bytes(content)
        output.append({'lang':'heb' if lang=='he' else 'eng','path':str(target), 'release_match':str(item.get('release','')).lower() in stem if item.get('release') else False})
        # Download only the preferred available language to respect account quotas.
        break
    return remember(cachekey,output,86400)


def probe(selected,key,deadline=None):
    raw_url=selected.get('local') or signed_path('raw',key)
    remaining=max(.1,deadline-time.monotonic()) if deadline is not None else 18
    result=subprocess.run(['ffprobe','-v','error','-rw_timeout','6000000','-analyzeduration','4000000','-probesize','4000000','-show_streams','-show_format','-of','json',raw_url],capture_output=True,timeout=min(18,remaining))
    if result.returncode: raise RuntimeError('Source probe failed')
    if selected.get('local'):return json.loads(result.stdout)
    start=min(1048576,max(0,selected['length']-32768))
    req=R.Request(raw_url,headers={'Range':f'bytes={start}-{start+16383}'})
    remaining=max(.1,deadline-time.monotonic()) if deadline is not None else 12
    with R.urlopen(req,timeout=min(12,remaining)) as response:
        if len(response.read(16384))<1024: raise RuntimeError('Source stalled during data preflight')
    return json.loads(result.stdout)


def prepared(key):
    with LOCKS_LOCK: lock=LOCKS.setdefault(key,threading.Lock())
    with lock:
        saved=cached('prepared:'+key)
        if saved: return saved
        with LIVE_LOCK:
            if LIVE['key'] not in (None,key) and LIVE['active']>0:
                raise RuntimeError('Another title is currently streaming; this server supports one stream')
        deadline=time.monotonic()+27
        for attempt in range(3):
            try:selected=resolve(key,deadline=deadline)
            except Exception:
                remember('source-status:'+key,{'state':'unavailable','checked_at':time.time()},900)
                raise
            try:
                info=probe(selected,key,deadline=deadline)
                video=next((s for s in info.get('streams',[]) if s.get('codec_type')=='video'),{})
                if video.get('codec_name') not in ('h264','mpeg4','vp8','vp9'):
                    raise RuntimeError('Source requires unsupported video decoding')
                break
            except Exception:
                if selected['hash']=='dd8255ecdc7ca55fb0bbf81323d87062db1f6d1c':
                    remember('open-film-failed',True,900)
                with db() as conn: conn.execute('DELETE FROM cache WHERE key=?',('resolved:'+key,))
                alternatives=[s for s in stream_candidates(key) if s['infoHash'].lower()!=selected['hash']]
                remember('streams:'+key,alternatives,900)
                try: torr({'action':'drop','hash':selected['hash']})
                except Exception: pass
                if attempt==2 or not alternatives:
                    remember('source-status:'+key,{'state':'unavailable','checked_at':time.time()},900)
                    raise
        tracks=[]; languages=set()
        for stream in info.get('streams',[]):
            typ=stream.get('codec_type')
            if typ not in ('video','audio','subtitle'): continue
            if typ=='video' and stream.get('disposition',{}).get('attached_pic'): continue
            lang=stream.get('tags',{}).get('language','und')
            lang={'he':'heb','iw':'heb','en':'eng'}.get(lang,lang)
            if typ=='subtitle': languages.add(lang)
            tracks.append({'Index':stream['index'],'Type':typ.title(),'Codec':stream.get('codec_name'),
                'Language':lang,'Channels':stream.get('channels'), 'Width':stream.get('width'),
                'Height':stream.get('height'),'IsExternal':False,
                'IsDefault':bool(stream.get('disposition',{}).get('default')),
                'IsForced':bool(stream.get('disposition',{}).get('forced'))})
        if not any(t['Type']=='Video' for t in tracks): raise RuntimeError('No video in selected source')
        # External search/download has a bounded deadline. A timeout never blocks video.
        if 'heb' not in languages:
            pool=ThreadPoolExecutor(max_workers=1)
            try:
                external=pool.submit(external_subtitles,key,selected['filename'],'eng' in languages).result(timeout=max(.1,min(15,deadline-time.monotonic())))
                for sub in external:
                    tracks.append({'Index':max(t['Index'] for t in tracks)+1,'Type':'Subtitle','Codec':'srt',
                        'Language':sub['lang'],'IsExternal':True,'SupportsExternalStream':True,
                        'DeliveryMethod':'External','DeliveryUrl':signed_path('sub',key)+'&lang='+sub['lang'],
                        'Title':sub['lang'].upper()+' (downloaded)'})
                    languages.add(sub['lang'])
            except Exception: log('Subtitle download unavailable or timed out for '+key)
            finally: pool.shutdown(wait=False,cancel_futures=True)
        preferred=next((t['Index'] for lang in ('heb','eng') for t in tracks if t['Type']=='Subtitle' and t['Language']==lang),None)
        output={'selected':selected,'MediaStreams':tracks,'DefaultSubtitleStreamIndex':preferred,
            'Container':Path(selected['filename']).suffix[1:].lower(),
            'RunTimeTicks':int(float(info.get('format',{}).get('duration',0))*10000000),
            'Bitrate':int(info.get('format',{}).get('bit_rate',5000000)),
            'languages':sorted(languages),'checked_at':time.time()}
        with db() as conn:
            conn.execute('INSERT OR REPLACE INTO playback VALUES (?,?,?)',(key,json.dumps(output),time.time()))
        kind,imdb,*_=key.split(':')
        with db() as conn: row=conn.execute('SELECT meta FROM items WHERE key=?',(kind+':'+imdb,)).fetchone()
        if row:
            folder=LIBRARY/('movies' if kind=='movie' else 'shows')/imdb
            try: make_poster(folder,json.loads(row[0]),kind,imdb)
            except OSError: log('Poster badge update deferred for '+key)
        return remember('prepared:'+key,output,300)


def update_collections():
    """Create native Jellyfin collections after its library scan has indexed items."""
    result=jf('/Items?Recursive=true&IncludeItemTypes=Movie,Series&Fields=Path,ProviderIds&Limit=2000')
    with db() as conn:
        rows=conn.execute('SELECT kind,imdb,buckets FROM items').fetchall()
    buckets={}
    for item in result.get('Items',[]):
        if not item.get('Path','').startswith('/catalogue/'): continue
        imdb=item.get('ProviderIds',{}).get('Imdb')
        kind='movie' if item['Type']=='Movie' else 'series'
        for row_kind,row_imdb,row_buckets in rows:
            if (kind,imdb)!=(row_kind,row_imdb): continue
            for bucket in json.loads(row_buckets):
                if bucket in ('Recent Releases','Popular'):
                    buckets.setdefault('Online '+bucket,[]).append(item['Id'])
    existing=jf('/Items?Recursive=true&IncludeItemTypes=BoxSet&Limit=100').get('Items',[])
    for name,ids in buckets.items():
        collection=next((i for i in existing if i['Name']==name),None)
        if not collection:
            collection=jf('/Collections?'+U.urlencode({'name':name}),{},method='POST')
        current=jf('/Items?'+U.urlencode({'ParentId':collection['Id'],'Limit':2000})).get('Items',[])
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

    def do_HEAD(self): self.do_GET()

    def do_GET(self):
        counted_play=False
        response_started=False
        try:
            parsed=U.urlsplit(self.path); parts=parsed.path.strip('/').split('/')
            if parsed.path=='/health':
                with db() as conn: count=conn.execute('SELECT kind,COUNT(*) FROM items GROUP BY kind').fetchall()
                return self.reply({'status':'ok','catalogue':dict(count),'subtitles_configured':True,
                    'subtitle_providers':['WizdomSubs']+(['OpenSubtitles'] if subtitle_config().get('api_key') else [])})
            if parts[0]=='poster' and len(parts)==3:
                kind,imdb=parts[1],parts[2].removesuffix('.jpg')
                if kind not in ('movie','series') or not re.fullmatch(r'tt\d{5,12}',imdb):
                    return self.reply({'error':'Not found'},404)
                folder=LIBRARY/('movies' if kind=='movie' else 'shows')/imdb
                folder.mkdir(parents=True,exist_ok=True)
                with POSTER_LIMIT, POSTER_LOCKS[int(imdb[2:])%len(POSTER_LOCKS)]:
                    with F.db() as conn:
                        row=conn.execute('SELECT meta FROM items WHERE kind=? AND imdb=?',(kind,imdb)).fetchone()
                        if not row:row=conn.execute('SELECT meta FROM discovery WHERE kind=? AND imdb=?',(kind,imdb)).fetchone()
                    meta=json.loads(row[0]) if row else {'poster':f'https://images.metahub.space/poster/medium/{imdb}/img'}
                    if not str(meta.get('poster','')).startswith('https://'):
                        meta['poster']=f'https://images.metahub.space/poster/medium/{imdb}/img'
                    make_poster(folder,meta,kind,imdb)
                    if not (folder/'poster.jpg').exists():return self.reply({'error':'Artwork unavailable'},404)
                    content=(folder/'poster.jpg').read_bytes()
                return self.reply(content,ctype='image/jpeg')
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
                if cached('streams:'+key)==[] and not F.local_download(key):
                    return self.reply({'streams':[]})
                # Warm source listings without making title details wait for an
                # indexer. No torrent/video is fetched until the user presses Play.
                if cached('streams:'+key) is None and not cached('listing-warm:'+key):
                    remember('listing-warm:'+key,True,30)
                    def warm_listing():
                        try: stream_candidates(key)
                        except Exception: log('Source listing unavailable for '+key)
                    threading.Thread(target=warm_listing,daemon=True).start()
                if cached('wizdom-search:'+key) is None and not cached('subtitle-warm:'+key):
                    remember('subtitle-warm:'+key,True,30)
                    def warm_subtitle_search():
                        try: wizdom_search(key)
                        except Exception: pass
                    threading.Thread(target=warm_subtitle_search,daemon=True).start()
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
            if not hmac.compare_digest(sig,signature(key)): return self.reply({'error':'Unauthorized'},401)
            if parts[0]=='info':
                info=prepared(key)
                return self.reply(info)
            if parts[0]=='sub':
                lang=U.parse_qs(parsed.query).get('lang',[''])[0]
                if lang not in ('heb','eng'): return self.reply({'error':'Not found'},404)
                target=DATA/'subtitles'/hashlib.sha256(key.encode()).hexdigest()[:24]/('he.srt' if lang=='heb' else 'en.srt')
                if not target.is_file(): return self.reply({'error':'Subtitle unavailable'},404)
                return self.reply(target.read_bytes(),ctype='application/x-subrip; charset=utf-8')
            selected=resolve(key)
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
            with R.urlopen(upstream_request,timeout=30) as response:
                self.send_response(response.status)
                response_started=True
                for name in ('Content-Type','Content-Length','Content-Range','Accept-Ranges','Last-Modified'):
                    if response.headers.get(name): self.send_header(name,response.headers[name])
                self.send_header('Connection','close')
                self.end_headers()
                if self.command!='HEAD':
                    while True:
                        chunk=response.read(128*1024)
                        if not chunk: break
                        self.wfile.write(chunk)
                        if parts[0]=='play':
                            with LIVE_LOCK: LIVE.update(key=key,touched=time.monotonic())
                self.close_connection=True
        except (BrokenPipeError,ConnectionResetError): pass
        except urllib.error.HTTPError as error:
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
            log('Request failed: '+type(error).__name__)
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
    parser.add_argument('command',choices=('serve','sync','collections','badges','trailers','discovery-sync','availability','downloads','imdb-sync'),default='serve',nargs='?')
    parser.add_argument('--movies',type=int,default=1000)
    parser.add_argument('--shows',type=int,default=200)
    args=parser.parse_args()
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
if __name__=='__main__': main()
