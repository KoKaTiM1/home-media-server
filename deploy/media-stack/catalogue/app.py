"""LAN-only catalogue and torrent playback adapter. Secrets never enter logs."""
import argparse
import base64
from contextlib import contextmanager
from concurrent.futures import ThreadPoolExecutor
import datetime as dt
from difflib import SequenceMatcher
import fcntl
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
from source_discovery import failure_state, success_state, search_parameters, torznab_streams, series_title_matches, tracker_sources, indexer_error_details
from features import Features
from source_failure_diagnostics import scope as failure_scope, record as record_source_failure, tag as diagnostic_tag
from metadata_jobs import MetadataJobs, MetadataBusy, InvalidSubmission, from_marker as metadata_coordinator
from reliability import Reliability, Checkpoint, SourceFailure, DecodeFailure, SourceStalled, SourceTooSlow, AUDIO_CODECS, VERSION, rank, METADATA_SECONDS, WORK_SECONDS
from embedded_subtitles import EmbeddedSubtitles
from playback_diagnostics import PlaybackDiagnostics
from russian_audio import RussianAudio
from verification_media import validate_video

DATA = Path(os.getenv('CATALOGUE_DATA', '/data'))
PROVIDER_QUEUE_GENERATION = DATA / 'provider-queue-generation'
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
POSTER_STYLE = '5'
STREAM_LIMIT = threading.Semaphore(1)
POSTER_LIMIT = threading.Semaphore(3)
POSTER_LOCKS = [threading.Lock() for _ in range(64)]
LIVE_LOCK = threading.Lock()
LIVE = {'key': None, 'touched': 0, 'active': 0}
DETAIL_LIMIT = threading.Semaphore(1)
DETAIL_SECONDS = 12
DETAIL_LOCK = threading.Lock()
DETAIL_RECENT = {}
BACKGROUND_TORRENTS = DATA / 'background-torrents.tsv'
FOREGROUND_CLEANUP_LOCK = threading.Lock()
FOREGROUND_CLEANUP = {'event': None, 'protected': set(), 'hashes': set()}



# One request budget follows nested network/probe operations. It is thread-local
# so a foreground waiter cannot change an in-flight background validation.
BUDGET = threading.local()
PREPARATION_SECONDS = 60
FIRST_DATA_SECONDS = 15
BACKGROUND_PROVIDER_SLOT_WAIT = 360


def provider_ticket_is_stale(mtime,protocol,now,generation):
    """Reap abandoned current tickets immediately and legacy tickets after safe grace."""
    if protocol=='locked-v1':return True
    if generation is not None and mtime<generation:return True
    return mtime<now-20*60


def provider_generation_time():
    try:return float(PROVIDER_QUEUE_GENERATION.read_text().strip())
    except (OSError,ValueError):return None


def initialize_provider_generation():
    """Fence tickets from processes that cannot survive this container start."""
    generation=time.time()
    temporary=PROVIDER_QUEUE_GENERATION.with_name(PROVIDER_QUEUE_GENERATION.name+f'.tmp-{os.getpid()}')
    try:
        temporary.write_text(f'{generation:.6f}\n')
        os.replace(temporary,PROVIDER_QUEUE_GENERATION)
    except OSError:
        try:temporary.unlink()
        except OSError:pass
        log('Provider queue generation marker unavailable; legacy grace retained')

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


def playback_error_details(error):
    """Return useful error context without exposing exception text or URLs."""
    details={'error_type':type(error).__name__}
    if isinstance(error,SourceTooSlow):details['error_category']='source_too_slow'
    elif isinstance(error,DecodeFailure):details['error_category']='decode_failure'
    elif isinstance(error,SourceStalled):details['error_category']='source_stalled'
    elif isinstance(error,SourceFailure):details['error_category']='source_failure'
    elif isinstance(error,Checkpoint):details['error_category']='checkpoint'
    elif isinstance(error,PlaybackBusy):details['error_category']='playback_busy'
    elif isinstance(error,urllib.error.HTTPError):
        details.update(error_category='upstream_http',http_status=int(error.code))
    elif isinstance(error,(ClientCancelled,BrokenPipeError,ConnectionResetError)):
        details['error_category']='client_disconnect'
    elif isinstance(error,(TimeoutError,subprocess.TimeoutExpired)):
        details['error_category']='timeout'
    elif isinstance(error,sqlite3.OperationalError):
        details['error_category']='local_database'
        # SQLite exposes stable numeric/type codes without requiring the raw
        # message, which can include filenames or query details. Keep only
        # lock/busy class diagnostics needed to distinguish contention.
        code=getattr(error,'sqlite_errorcode',None)
        name=getattr(error,'sqlite_errorname',None)
        if isinstance(code,int):details['sqlite_errorcode']=code
        if isinstance(name,str) and re.fullmatch(r'SQLITE_[A-Z0-9_]{1,64}',name):
            details['sqlite_errorname']=name
    elif isinstance(error,urllib.error.URLError):
        details['error_category']='network'
    elif isinstance(error,OSError):
        details['error_category']='local_io'
        if isinstance(error.errno,int):details['error_errno']=error.errno
    else:details['error_category']='operation'
    return details


def playback_event(key,phase,started,outcome,error=None):
    # Log only validated title keys and sanitized error fields. Never log
    # exception strings: HTTP and ffmpeg errors can contain URLs or credentials.
    if not re.fullmatch(r'(movie:tt\d{5,12}|series:tt\d{5,12}:\d+:\d+)',key):return
    event={'key':key,'phase':phase,'seconds':round(time.monotonic()-started,3),
           'outcome':outcome,**(playback_error_details(error) if error else
                               {'error_type':None,'error_category':None})}
    log('Playback '+json.dumps(event,sort_keys=True))
    if not getattr(BUDGET,'validation_background',False) and not getattr(BUDGET,'diagnostic_internal',False):
        try:DIAG.record(event)
        except (OSError,ValueError,sqlite3.Error):log('Playback diagnostics temporarily unavailable')


def playback_failure(key,error):
    # A failed HTTP request or unknown error cannot invalidate sampled media
    # evidence. Only typed source/decode evidence requests a fresh background
    # check; its pending fence prevents any foreground pass clearing that hold.
    try:return DIAG.recheck(key,error)
    except (OSError,ValueError,sqlite3.Error):
        log('Playback recheck scheduling deferred');return False


@contextmanager
def playback_phase(key,phase):
    started=time.monotonic()
    if getattr(BUDGET,'remote_verification',False) and phase in ('preparation','torrent_resolution','video_probe'):
        from verification_remote import producer_phase
        producer_phase(globals(),key,phase)
    try:
        yield
    except Exception as error:
        record_source_failure(BUDGET,error,phase)
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


def prune_expired_cache(limit=20000,batch_size=500,now=None):
    """Delete expired temporary cache rows in bounded SQLite transactions.

    Verification, retry, playback, and audit state live in separate tables and
    are never selected by this maintenance routine. SQLite reuses the freed
    pages; this routine deliberately does not run VACUUM or rewrite the DB.
    """
    if type(limit) is not int or not 1<=limit<=50000:
        raise ValueError('Cache prune limit must be between 1 and 50000')
    if type(batch_size) is not int or not 1<=batch_size<=1000:
        raise ValueError('Cache prune batch size must be between 1 and 1000')
    now=time.time() if now is None else float(now)
    DATA.mkdir(parents=True,exist_ok=True)
    with (DATA/'cache-prune.lock').open('a') as lock:
        try:fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        except BlockingIOError:return 0
        conn=db()
        try:
            conn.execute('PRAGMA busy_timeout=2000')
            # This one-time index makes both batch selection and later daily
            # runs bounded by expired rows instead of rescanning the cache.
            conn.execute('CREATE INDEX IF NOT EXISTS cache_expiry ON cache(expires) WHERE expires IS NOT NULL')
            conn.commit()
            deleted=0
            while deleted<limit:
                count=min(batch_size,limit-deleted)
                conn.execute('BEGIN IMMEDIATE')
                rowids=[row[0] for row in conn.execute(
                    'SELECT rowid FROM cache WHERE expires IS NOT NULL AND expires<=? ORDER BY expires,rowid LIMIT ?',
                    (now,count)).fetchall()]
                if not rowids:
                    conn.commit();break
                before=conn.total_changes
                conn.executemany('DELETE FROM cache WHERE rowid=?',((rowid,) for rowid in rowids))
                changed=conn.total_changes-before
                conn.commit()
                deleted+=changed
            return deleted
        finally:conn.close()


class SourceProviderWait(Checkpoint):
    def __init__(self,message,provider=None,reason=None,retry_after=None):
        super().__init__(message);self.provider=provider;self.reason=reason;self.retry_after=retry_after


def provider_capacity_retry_after(queue_slots,spacing_seconds=15,jitter_seconds=0):
    """Estimate how long an overflow caller should wait for a paced FIFO."""
    if type(queue_slots) is not int or not 1<=queue_slots<=32:
        raise ValueError('Invalid provider queue capacity')
    if type(spacing_seconds) is not int or spacing_seconds<1:
        raise ValueError('Invalid provider pacing interval')
    if type(jitter_seconds) is not int or not 0<=jitter_seconds<=15:
        raise ValueError('Invalid provider retry jitter')
    return max(30,queue_slots*spacing_seconds)+jitter_seconds


@contextmanager
def serialized_provider_slot(lock_path,control_key,priority=0,queue_slots=None,queue_spacing_seconds=15,wait_seconds=None):
    # All source providers serialize across processes. Foreground calls fail
    # fast; background callers take a filesystem ticket so the 16 producer
    # lanes cannot repeatedly race one another for the next provider slot.
    # The existing provider pacing and cooldown remain authoritative.
    background=bool(getattr(BUDGET,'validation_background',False))
    deadline=time.monotonic()
    if background:
        # There are up to 16 producer lanes. Provider calls remain serialized
        # and paced (15s fallback/5s primary). A speed-rejected source gets a
        # priority ticket so its alternative lookup is not trapped behind
        # ordinary empty-result lookups; FIFO is retained within each class.
        deadline+=min(BACKGROUND_PROVIDER_SLOT_WAIT,wait_seconds) if wait_seconds is not None else BACKGROUND_PROVIDER_SLOT_WAIT
        for name in ('worker_deadline','deadline'):
            limit=getattr(BUDGET,name,None)
            if limit is not None:deadline=min(deadline,limit)
    state=cached(control_key) or {}
    cooldown=state.get('cooldown',0)-time.time()
    if cooldown>0:raise SourceProviderWait('Source provider cooldown active',control_key,'cooldown',cooldown)
    ticket=None;ticket_fd=None;queue_dir=None
    if background:
        queue_dir=Path(str(lock_path)+'.waiters')
        try:
            queue_dir.mkdir(parents=True,exist_ok=True)
            guard=Path(str(queue_dir)+'.guard').open('a')
            try:
                fcntl.flock(guard,fcntl.LOCK_EX)
                stamp=f'{time.time_ns():020d}'
                ticket_name=(f'p1-{stamp}-{uuid.uuid4().hex}.ticket' if priority>0
                             else f'{stamp}-{uuid.uuid4().hex}.ticket')
                ticket=queue_dir/ticket_name
                ticket_fd=os.open(ticket,os.O_CREAT|os.O_EXCL|os.O_RDWR,0o660)
                os.write(ticket_fd,b'locked-v1')
                # The descriptor lock marks ownership. The queue guard makes
                # ticket creation and orphan scans atomic with respect to one
                # another, so a just-created ticket cannot be reaped early.
                fcntl.flock(ticket_fd,fcntl.LOCK_EX)
            finally:
                fcntl.flock(guard,fcntl.LOCK_UN);guard.close()
        except OSError:
            # A transient ticket-directory error must not make source lookup
            # unavailable; retain the prior serialized-lock behavior.
            if ticket_fd is not None:
                os.close(ticket_fd);ticket_fd=None
            if ticket is not None:
                try:ticket.unlink()
                except OSError:pass
            ticket=None
    def ticket_turn():
        if ticket is None:return True
        guard=None
        try:
            guard=Path(str(queue_dir)+'.guard').open('a')
            fcntl.flock(guard,fcntl.LOCK_EX)
            entries=[]
            # Legacy tickets did not hold descriptor locks. Preserve recent
            # live legacy tickets during a code handoff, but a container-start
            # generation proves pre-start owners were killed. New-format
            # unlocked tickets are always abandoned and can be reaped now.
            now=time.time();generation=provider_generation_time()
            for entry in queue_dir.glob('*.ticket'):
                if entry==ticket:
                    entries.append(entry);continue
                fd=None
                try:
                    fd=os.open(entry,os.O_RDWR)
                    try:fcntl.flock(fd,fcntl.LOCK_EX|fcntl.LOCK_NB)
                    except BlockingIOError:
                        entries.append(entry)
                    else:
                        try:
                            st=entry.stat();protocol=os.read(fd,32).decode('ascii','ignore')
                            if provider_ticket_is_stale(st.st_mtime,protocol,now,generation):entry.unlink()
                            else:entries.append(entry)
                        except FileNotFoundError:pass
                        fcntl.flock(fd,fcntl.LOCK_UN)
                except FileNotFoundError:pass
                except OSError:entries.append(entry)
                finally:
                    if fd is not None:os.close(fd)
            def arrival_order(entry):
                name=entry.name
                raw=name[3:23] if name.startswith('p1-') else name.split('-',1)[0]
                try:return (int(raw),name)
                except ValueError:return (entry.stat().st_mtime_ns,name)
            # Keep the oldest admitted callers. Overflow releases its ticket
            # promptly so it can prepare other titles while this queue drains.
            # Priority affects service order, never evicts admitted callers.
            if queue_slots is not None and ticket not in sorted(entries,key=arrival_order)[:queue_slots]:
                # The caller was never admitted to the provider FIFO. Let the
                # admitted slots drain before retrying instead of waking every
                # 30 seconds into the same full queue. Hash only the random
                # ticket name so simultaneous overflow callers spread out.
                jitter=int.from_bytes(hashlib.sha256((ticket.name if ticket else control_key).encode()).digest()[:2],'big')%16
                retry_after=provider_capacity_retry_after(queue_slots,queue_spacing_seconds,jitter)
                raise SourceProviderWait('Source provider queue is full',control_key,'capacity',retry_after)
            priority_state=queue_dir/'priority-served'
            try:priority_served=priority_state.read_text().strip()=='1'
            except OSError:priority_served=False
            has_priority=any(entry.name.startswith('p1-') for entry in entries)
            has_normal=any(not entry.name.startswith('p1-') for entry in entries)
            def queue_order(entry):
                name=entry.name
                if name.startswith('p1-'):
                    group=1 if priority_served and has_normal else 0
                    try:return (group,int(name[3:23]),name)
                    except ValueError:return (group,0,name)
                group=0 if has_priority and has_normal and priority_served else 1
                try:return (group,int(name.split('-',1)[0]),name)
                except ValueError:return (group,entry.stat().st_mtime_ns,name)
            return bool(entries) and min(entries,key=queue_order)==ticket
        except OSError:return True
        finally:
            if guard is not None:
                try:fcntl.flock(guard,fcntl.LOCK_UN)
                finally:guard.close()
    def wait_slice(seconds):
        cancel=getattr(BUDGET,'cancel',None)
        if cancel and cancel():raise ClientCancelled('Client disconnected')
        time.sleep(min(.5,seconds))
    try:
        while True:
            if background and not ticket_turn():
                wait=deadline-time.monotonic()
                if wait<=0:raise SourceProviderWait('Source provider slot wait budget exhausted',control_key,'queue_timeout') from None
                wait_slice(wait);continue
            lock=Path(lock_path).open('a')
            try:
                try:fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
                except BlockingIOError:
                    lock.close()
                    if not background:raise SourceProviderWait('Source provider request already active',control_key,'busy') from None
                    wait=deadline-time.monotonic()
                    if wait<=0:raise SourceProviderWait('Source provider slot wait budget exhausted',control_key,'queue_timeout') from None
                    wait_slice(wait);continue
                state=cached(control_key) or {}
                now=time.time()
                if state.get('cooldown',0)>now:
                    lock.close()
                    raise SourceProviderWait('Source provider cooldown active',control_key,'cooldown',max(1,state['cooldown']-now))
                spacing=state.get('next',0)-now
                if spacing>0:
                    lock.close()
                    if not background:raise SourceProviderWait('Source provider pacing interval active',control_key,'pacing')
                    wait=deadline-time.monotonic()
                    if wait<=0:raise SourceProviderWait('Source provider slot wait budget exhausted',control_key,'queue_timeout') from None
                    wait_slice(min(spacing,wait));continue
                break
            except BaseException:
                if not lock.closed:lock.close()
                raise
        try:yield
        finally:
            if ticket is not None:
                # Let a speed-rejected-source fallback jump ahead once, then
                # give the oldest ordinary fallback its turn if one is queued.
                # This keeps urgent alternatives responsive without allowing
                # a continuing priority backlog to starve ordinary lookups.
                try:
                    fairness=queue_dir/'priority-served'
                    temporary=queue_dir/f'.priority-served-{os.getpid()}-{uuid.uuid4().hex}.tmp'
                    temporary.write_text('1' if priority>0 else '0')
                    os.replace(temporary,fairness)
                except OSError:pass
            fcntl.flock(lock,fcntl.LOCK_UN)
            lock.close()
    finally:
        if ticket is not None:
            guard=None
            try:
                guard=Path(str(queue_dir)+'.guard').open('a')
                fcntl.flock(guard,fcntl.LOCK_EX)
                try:ticket.unlink()
                except FileNotFoundError:pass
            except OSError:pass
            finally:
                if guard is not None:
                    try:fcntl.flock(guard,fcntl.LOCK_UN)
                    finally:guard.close()
                if ticket_fd is not None:
                    try:fcntl.flock(ticket_fd,fcntl.LOCK_UN)
                    finally:os.close(ticket_fd)


def provider_failed(control_key, error, spacing):
    status=error.code if isinstance(error,urllib.error.HTTPError) else 0
    headers=getattr(error,'headers',None)
    jitter=int.from_bytes(os.urandom(2),'big')%31
    state=failure_state(cached(control_key),status,headers,spacing,time.time(),jitter)
    for name in ('adapter_http_status','indexer_failure','upstream_http_status'):state.pop(name,None)
    details=getattr(error,'indexer_details',{})
    if isinstance(details,dict):
        if details.get('adapter_http_status')==status and type(status) is int:state['adapter_http_status']=status
        if details.get('indexer_failure') in ('unknown','connection_reset','upstream_http_429'):state['indexer_failure']=details['indexer_failure']
        if details.get('upstream_http_status')==429:state['upstream_http_status']=429
    remember(control_key,state,max(90000,state['cooldown']-time.time()+60))


def provider_succeeded(control_key,spacing):
    state=success_state(cached(control_key),spacing,time.time())
    for name in ('adapter_http_status','indexer_failure','upstream_http_status'):state.pop(name,None)
    remember(control_key,state,90000)


@contextmanager
def source_provider_slot():
    with serialized_provider_slot(DATA/'source-provider.lock','source-provider-control'):
        try:yield
        except (urllib.error.URLError,TimeoutError,ValueError) as error:
            provider_failed('source-provider-control',error,5)
            raise
        else:provider_succeeded('source-provider-control',5)


def fetch(url, data=None, headers=None, timeout=20, raw=False, method=None):
    if U.urlsplit(url).hostname==U.urlsplit(PROVIDER).hostname:
        with source_provider_slot():
            return fetch_raw(url,data,headers,timeout,raw,method)
    return fetch_raw(url,data,headers,timeout,raw,method)


def fetch_raw(url, data=None, headers=None, timeout=20, raw=False, method=None):
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


def torrent_metadata_db():
    conn=db()
    conn.execute('CREATE TABLE IF NOT EXISTS torrent_metadata (hash TEXT PRIMARY KEY, requested REAL DEFAULT 0, next_try REAL DEFAULT 0, updated REAL DEFAULT 0)')
    return conn


def exact_torrent_file(status,selected):
    return (status.get('hash','').lower()==selected.get('hash','').lower()
            and any(f.get('id')==selected.get('index') and f.get('path')==selected.get('filename')
                    and f.get('length')==selected.get('length') for f in status.get('file_stats',[])))


def retain_torrent_metadata(selected,status=None):
    """Request native InfoBytes persistence, separately from media proof state."""
    if selected.get('local'):return False
    infohash=selected.get('hash','').lower()
    if not re.fullmatch('[a-f0-9]{40}',infohash):return False
    try:
        with torrent_metadata_db() as conn:
            row=conn.execute('SELECT requested FROM torrent_metadata WHERE hash=?',(infohash,)).fetchone()
        if row and row[0]>time.time()-86400:return True
        status=status or torr({'action':'get','hash':infohash},timeout=2)
        if not exact_torrent_file(status,selected):return False
        result=torr({'action':'add','link':selected.get('link') or selected.get('torrent_url') or 'magnet:?xt=urn:btih:'+infohash,
                     'save_to_db':True},timeout=2)
        if not exact_torrent_file(result or {},selected):return False
        with torrent_metadata_db() as conn:
            conn.execute('INSERT OR REPLACE INTO torrent_metadata VALUES (?,?,?,?)',
                         (infohash,time.time(),time.time()+86400,time.time()))
        # TorrServer saves asynchronously. This is an acknowledged request,
        # not independent proof of disk durability or playable media.
        log('Torrent metadata persistence requested')
        return True
    except (OSError,ValueError,sqlite3.Error,urllib.error.URLError):
        log('Torrent metadata persistence deferred');return False


def backfill_torrent_metadata():
    """One exact verified source per invocation; no video download or re-audit."""
    with (DATA/'torrent-metadata.lock').open('a') as lock:
        try:fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        except BlockingIOError:return 0
        if F.foreground_busy() or detail_playback_busy():return 0
        with torrent_metadata_db() as conn:
            rows=conn.execute("SELECT p.key,p.value FROM playback p JOIN validation v ON v.key=p.key "
                "LEFT JOIN torrent_metadata t ON t.hash=json_extract(p.value,'$.selected.hash') "
                "WHERE v.state='verified' AND json_extract(p.value,'$.selected.local') IS NULL "
                "AND COALESCE(t.next_try,0)<=? ORDER BY COALESCE(t.updated,0),p.updated DESC LIMIT 1",(time.time(),)).fetchall()
        if not rows:return 0
        key,raw=rows[0];selected=json.loads(raw)['selected'];infohash=selected['hash']
        # Mark before doing work so interrupted attempts cannot monopolize it.
        with torrent_metadata_db() as conn:
            conn.execute('INSERT INTO torrent_metadata VALUES (?,0,?,?) ON CONFLICT(hash) DO UPDATE SET next_try=excluded.next_try,updated=excluded.updated',
                         (infohash,time.time()+900,time.time()))
        def cancel():return F.foreground_busy() or detail_playback_busy()
        try:
            with request_budget(time.monotonic()+55,cancel):
                evidence=durable_playback_evidence(key)
                if not evidence or REL.sid(evidence['selected'])!=REL.sid(selected):return 0
                with REL.lock('torrent:'+infohash):
                    record_background_torrent(key,infohash,force=True)
                    status=None
                    try:status=torr({'action':'get','hash':infohash},timeout=2)
                    except urllib.error.HTTPError as error:
                        if error.code!=404:raise
                    if not status or not status.get('file_stats'):
                        status=torr({'action':'add','link':selected.get('link') or selected.get('torrent_url') or 'magnet:?xt=urn:btih:'+infohash,'save_to_db':False},timeout=2)
                    while not status.get('file_stats'):
                        time.sleep(remaining(1))
                        status=torr({'action':'get','hash':infohash},timeout=2)
                    return int(retain_torrent_metadata(selected,status))
        except (OSError,ValueError,sqlite3.Error,urllib.error.URLError,ClientCancelled,Checkpoint):
            log('Torrent metadata backfill deferred');return 0


def record_background_torrent(key,infohash,force=False):
    """Remember server TorrServer work that may be discarded for playback."""
    if not force and not getattr(BUDGET,'validation_background',False):return
    if not re.fullmatch(r'(?:movie:tt\d{5,12}|series:tt\d{5,12}:\d+:\d+)',key):return
    if not re.fullmatch(r'[a-f0-9]{40}',infohash):return
    DATA.mkdir(parents=True,exist_ok=True)
    with BACKGROUND_TORRENTS.open('a+',encoding='ascii') as ledger:
        fcntl.flock(ledger,fcntl.LOCK_EX)
        ledger.write(f'{time.time():.3f}\t{key}\t{infohash}\n')
        ledger.flush()
        fcntl.flock(ledger,fcntl.LOCK_UN)


def background_torrent_records():
    if not BACKGROUND_TORRENTS.is_file():return []
    with BACKGROUND_TORRENTS.open('a+',encoding='ascii') as ledger:
        fcntl.flock(ledger,fcntl.LOCK_SH)
        ledger.seek(0);lines=ledger.readlines()
        fcntl.flock(ledger,fcntl.LOCK_UN)
    records=[]
    for line in lines[-4096:]:
        try:
            created,key,infohash=line.rstrip('\n').split('\t')
            created=float(created)
        except ValueError:continue
        if (re.fullmatch(r'(?:movie:tt\d{5,12}|series:tt\d{5,12}:\d+:\d+)',key)
            and re.fullmatch(r'[a-f0-9]{40}',infohash)):
            records.append((created,key,infohash))
    return records


def retain_background_torrent_records(removed):
    if not removed or not BACKGROUND_TORRENTS.is_file():return
    with BACKGROUND_TORRENTS.open('a+',encoding='ascii') as ledger:
        fcntl.flock(ledger,fcntl.LOCK_EX)
        ledger.seek(0);lines=ledger.readlines();ledger.seek(0);ledger.truncate()
        for line in lines[-4096:]:
            try:infohash=line.rstrip('\n').split('\t')[2]
            except (ValueError,IndexError):continue
            if infohash not in removed:ledger.write(line)
        ledger.flush();fcntl.flock(ledger,fcntl.LOCK_UN)


def foreground_source_hashes(key):
    """Return persisted physical sources belonging to a foreground title."""
    values=[]
    try:
        with db() as conn:
            row=conn.execute('SELECT value FROM playback WHERE key=?',(key,)).fetchone()
            if row:values.append(json.loads(row[0]))
            row=conn.execute('SELECT value FROM cache WHERE key=? AND expires>?',('resolved:'+key,time.time())).fetchone()
            if row:values.append(json.loads(row[0]))
    except (OSError,ValueError,sqlite3.Error):return set()
    hashes=set()
    for value in values:
        selected=value.get('selected',value) if isinstance(value,dict) else {}
        infohash=selected.get('hash','').lower() if isinstance(selected,dict) else ''
        if re.fullmatch(r'[a-f0-9]{40}',infohash):hashes.add(infohash)
    return hashes


def cleanup_background_torrents():
    records=background_torrent_records()
    by_hash={}
    for created,key,infohash in records:by_hash.setdefault(infohash,[]).append((created,key))
    with FOREGROUND_CLEANUP_LOCK:
        protected=set(FOREGROUND_CLEANUP['protected'])
        protected_hashes=set(FOREGROUND_CLEANUP['hashes'])
    candidates=[infohash for infohash,owners in by_hash.items()
                if infohash not in protected_hashes and not any(key in protected for _,key in owners)]
    removed=set();outcomes={'dropped':0,'missing':0,'active_readers':0,'protected':0,'api_error':0}
    def discard(infohash):
        with FOREGROUND_CLEANUP_LOCK:
            current=set(FOREGROUND_CLEANUP['protected'])
            current_hashes=set(FOREGROUND_CLEANUP['hashes'])
        if infohash in current_hashes or any(key in current for _,key in by_hash[infohash]):return 'protected'
        try:
            # Cache inspection is non-activating on the pinned native build.
            # A dormant saved record has no readers and needs no drop request.
            for _ in range(2):
                try:cache=fetch(TORR+'/cache',{'action':'get','hash':infohash},torr_headers(),timeout=.75) or {}
                except urllib.error.HTTPError as error:
                    if error.code==404:return 'missing'
                    raise
                if cache.get('Readers'):return 'active_readers'
            with FOREGROUND_CLEANUP_LOCK:
                if infohash in FOREGROUND_CLEANUP['hashes'] or any(key in FOREGROUND_CLEANUP['protected'] for _,key in by_hash[infohash]):return 'protected'
            torr({'action':'drop','hash':infohash},timeout=.75)
            return 'dropped'
        except (OSError,TimeoutError,ValueError,urllib.error.URLError):return 'api_error'
    with ThreadPoolExecutor(max_workers=8) as pool:
        for infohash,outcome in zip(candidates,pool.map(discard,candidates)):
            if outcome in ('dropped','missing'):removed.add(infohash)
            if outcome in outcomes:outcomes[outcome]+=1
            else:outcomes['api_error']+=1
    retain_background_torrent_records(removed)
    if records:log('Background torrent cleanup tracked={} candidates={} dropped={} missing={} active_readers={} protected={} api_errors={}'.format(
        len(records),len(candidates),outcomes['dropped'],outcomes['missing'],outcomes['active_readers'],outcomes['protected'],outcomes['api_error']))


def start_foreground_cleanup(key):
    source_hashes=foreground_source_hashes(key)
    with LIVE_LOCK:
        active_key=LIVE['key'] if LIVE['active']>0 else None
    if active_key:source_hashes.update(foreground_source_hashes(active_key))
    with FOREGROUND_CLEANUP_LOCK:
        FOREGROUND_CLEANUP['protected'].add(key)
        if active_key:FOREGROUND_CLEANUP['protected'].add(active_key)
        FOREGROUND_CLEANUP['hashes'].update(source_hashes)
        event=FOREGROUND_CLEANUP['event']
        if event is not None and not event.is_set():return event
        event=threading.Event()
        FOREGROUND_CLEANUP['event']=event
    def work():
        try:cleanup_background_torrents()
        finally:
            with FOREGROUND_CLEANUP_LOCK:
                FOREGROUND_CLEANUP['protected'].clear()
                FOREGROUND_CLEANUP['hashes'].clear()
            event.set()
    threading.Thread(target=work,daemon=True).start()
    return event


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


def metadata_pool_config():
    defaults={'enabled':False,'concurrency':2,'high':256,'low':128}
    try:values=json.loads((DATA/'metadata-preparation.json').read_text())
    except FileNotFoundError:return defaults
    cfg={**defaults,**values}
    if type(cfg['enabled']) is not bool or not 1<=cfg['concurrency']<=4 or not 0<cfg['low']<cfg['high']<=4096:
        raise ValueError('Invalid preparation pool configuration')
    return cfg


def metadata_prepare_stop(slot):
    (DATA/('metadata-prepare-'+str(slot)+'.stop')).touch()
    deadline=time.monotonic()+30
    with (DATA/('metadata-prepare-'+str(slot)+'.lock')).open('a') as lock:
        while True:
            try:fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB);return
            except BlockingIOError:
                if time.monotonic()>=deadline:raise RuntimeError('Metadata preparation stop is still pending') from None
                time.sleep(.1)


def metadata_prepare(slot):
    stop=DATA/('metadata-prepare-'+str(slot)+'.stop')
    with (DATA/('metadata-prepare-'+str(slot)+'.lock')).open('a') as lock:
        try:fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        except BlockingIOError:raise RuntimeError('Metadata preparation slot is still stopping') from None
        stop.unlink(missing_ok=True);next_session=0;busy=True;last_status=0;last_phase=None
        while not stop.exists():
            cfg=metadata_pool_config()
            if not cfg['enabled'] or slot>cfg['concurrency']:return
            phase='idle';sleep_for=1
            try:
                if time.monotonic()>=next_session:
                    busy=F.foreground_busy() or any(x.get('NowPlayingItem') for x in jf('/Sessions',timeout=1))
                    next_session=time.monotonic()+5
                if busy or F.foreground_busy():phase='playback pause';sleep_for=5
                else:
                    metadata_import()
                    F.resume_metadata_dependencies()
                    fetched=fetch_waiting_metadata(metadata_coordinator(DATA))
                    metadata_import()
                    if fetched is True:phase='metadata imported';sleep_for=1
                    elif fetched is False:phase='provider or queue wait';sleep_for=3
                    else:phase='idle';sleep_for=10
            except Exception as error:
                phase='retry '+type(error).__name__;sleep_for=3
                log('Metadata pool checkpoint '+type(error).__name__)
                next_session=0
            # The pool's provider slot is paced at three seconds. When there
            # is no prerequisite to fetch, avoid four idle workers repeatedly
            # reopening the catalogue and writing heartbeats every second.
            # Publish phase changes immediately and refresh idle heartbeats
            # every ten seconds, below the monitor's stale-worker threshold.
            now=time.monotonic()
            if phase!=last_phase or now-last_status>=10:
                with F.db() as conn:
                    conn.execute('INSERT OR REPLACE INTO metadata_preparation_workers VALUES (?,?,?)',(slot,time.time(),phase))
                last_status=now;last_phase=phase
            time.sleep(sleep_for)
        with F.db() as conn:conn.execute('DELETE FROM metadata_preparation_workers WHERE slot=?',(slot,))


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
        if metadata_pool_config()['enabled'] and getattr(BUDGET,'preparation_key',None):
            F.metadata_admission_blocked(BUDGET.preparation_key)
            raise MetadataPending('Metadata queued for preparation pool')
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
            try:ack=jobs.receive('server','server',lease,result)
            except InvalidSubmission:
                jobs.release(lease)
                raise MetadataPending('Provider catalogue metadata unavailable') from None
            if ack not in ('accepted','duplicate'):
                raise Checkpoint('Metadata lease expired; work retained for retry')
            metadata_import(limit=1,key=kind+':'+imdb)
            saved=cached(key)
            if saved is None:raise Checkpoint('Metadata import awaiting retry')
        else:remember(key,saved,21600)
    saved=F.canonical_movie_year(kind,imdb,saved)
    F.record_dates(kind,[saved])
    return F.localize(saved)


def metadata_retry_category(error,stage):
    """Return a low-cardinality reason and HTTP code without exception text."""
    if isinstance(error,MetadataResponseShapeError):return 'invalid_response',0
    if isinstance(error,urllib.error.HTTPError):
        status=error.code if type(error.code) is int and 100<=error.code<=599 else 0
        return 'upstream_http',status
    if stage=='provider_fetch' and isinstance(error,json.JSONDecodeError):return 'invalid_json',0
    if stage=='provider_envelope' and isinstance(error,(KeyError,TypeError,ValueError)):return 'invalid_envelope',0
    if stage=='coordinator_receipt' and isinstance(error,InvalidSubmission):
        receipt_reasons={
            'Invalid worker identity':'receipt_worker_identity',
            'Token is bound to another worker':'receipt_owner_mismatch',
            'Token permits series metadata only':'receipt_scope_mismatch',
            'Invalid result identity':'receipt_invalid_result_identity',
            'Result identity reused with different ownership or data':'receipt_result_conflict',
            'Invalid metadata result':'receipt_invalid_metadata',
            'Result metadata identity mismatch':'receipt_metadata_identity_mismatch',
            'Invalid cooldown':'receipt_invalid_cooldown',
        }
        reason=error.args[0] if error.args and isinstance(error.args[0],str) else ''
        return receipt_reasons.get(reason,'invalid_receipt'),0
    if stage=='coordinator_receipt' and isinstance(error,(KeyError,TypeError,ValueError)):return 'invalid_receipt',0
    if stage=='retry_receipt':return 'retry_receipt_error',0
    if isinstance(error,(TimeoutError,socket.timeout)):
        return 'timeout',0
    if isinstance(error,urllib.error.URLError):
        return ('timeout',0) if isinstance(error.reason,(TimeoutError,socket.timeout)) else ('network',0)
    if isinstance(error,OSError):
        return 'network',0
    if isinstance(error,(ValueError,TypeError,KeyError)):
        return 'invalid_response',0
    return 'other',0


class MetadataResponseShapeError(ValueError):
    """Sanitized discriminator for common Cinemeta envelope/identity defects."""
    def __init__(self,code):
        self.code=code
        super().__init__('Invalid metadata response')


def record_metadata_retry(error,stage):
    category,status=metadata_retry_category(error,stage)
    if isinstance(error,MetadataResponseShapeError):
        error_type=error.code
    else:
        error_type=type(error).__name__
    allowed_types={'HTTPError','URLError','TimeoutError','ConnectionError','OSError','JSONDecodeError',
                   'KeyError','TypeError','ValueError','InvalidSubmission','MetadataBusy','OperationalError',
                   'missing_meta','invalid_meta','id_mismatch','type_mismatch','id_type_mismatch',
                   'missing_name','invalid_name'}
    if error_type not in allowed_types:error_type='Other'
    hour=int(time.time()//3600)
    try:
        with F.db() as conn:
            conn.execute('''INSERT INTO metadata_retry_counts(hour,category,status,attempts) VALUES (?,?,?,1)
                ON CONFLICT(hour,category,status) DO UPDATE SET attempts=attempts+1''',(hour,category,status))
            conn.execute('''INSERT INTO metadata_retry_details(hour,stage,category,error_type,status,attempts) VALUES (?,?,?,?,?,1)
                ON CONFLICT(hour,stage,category,error_type,status) DO UPDATE SET attempts=attempts+1''',
                (hour,stage,category,error_type,status))
            conn.execute('DELETE FROM metadata_retry_counts WHERE hour<?',(hour-167,))
            conn.execute('DELETE FROM metadata_retry_details WHERE hour<?',(hour-167,))
    except sqlite3.Error:
        # Retry telemetry must never change the metadata retry outcome.
        pass
    detail=f' status={status}' if status else ''
    log(f'Metadata dependency fetch retry stage={stage} category={category} type={error_type}{detail}')


def fetch_waiting_metadata(jobs):
    """One prerequisite, with ordinary provider/lease fences."""
    now=time.time()
    if metadata_pool_config()['enabled']:
        with jobs.db() as provider:
            if max(jobs.value(provider,'provider_after'),jobs.value(provider,'provider_slot'),jobs.value(provider,'preparation_yield_until'))>now:return False
    with F.db() as conn:
        if not conn.execute("SELECT 1 FROM verification_metadata_dependencies WHERE state='waiting' LIMIT 1").fetchone() and not conn.execute('SELECT 1 FROM metadata_preparation_requests LIMIT 1').fetchone():return None
        conn.execute('BEGIN IMMEDIATE')
        conn.execute("DELETE FROM metadata_preparation_requests WHERE NOT EXISTS(SELECT 1 FROM validation v WHERE v.key=metadata_preparation_requests.key AND v.checked=metadata_preparation_requests.validation_checked AND v.state!='downloaded')")
        row=conn.execute("""SELECT d.metadata_key FROM (
                SELECT key,metadata_key,waiting,validation_checked FROM verification_metadata_dependencies WHERE state='waiting'
                UNION ALL SELECT key,metadata_key,created,validation_checked FROM metadata_preparation_requests
            ) d
            LEFT JOIN verification_metadata_fetch f ON f.metadata_key=d.metadata_key
            JOIN validation v ON v.key=d.key
            WHERE v.checked=d.validation_checked AND v.state!='downloaded'
            AND COALESCE(f.next_fetch,0)<=?
            AND NOT EXISTS(SELECT 1 FROM cache c WHERE c.key=d.metadata_key AND c.expires>?)
            AND NOT EXISTS(SELECT 1 FROM verification_holds h WHERE (h.key=d.key OR h.key=substr(d.metadata_key,6)) AND h.expires>?)
            AND NOT EXISTS(SELECT 1 FROM verification_retry_waits r WHERE (r.key=d.key OR r.key=substr(d.metadata_key,6)) AND r.next_retry>?)
            AND NOT EXISTS(SELECT 1 FROM preparation_work p WHERE p.key=substr(d.metadata_key,6))
            ORDER BY CASE WHEN EXISTS(SELECT 1 FROM manual_rechecks m WHERE m.key=d.key AND m.status='pending') THEN 0 WHEN COALESCE(json_extract(v.proof,'$.version'),0)=5 THEN 1 ELSE 2 END,COALESCE(f.next_fetch,0),d.waiting LIMIT 1""",(now,now,now,now)).fetchone()
        if not row:return None
        meta=row[0]
        # Separate from readiness polling; concurrent timer invocations cannot
        # pick this parent, and blocked parents cannot starve the rest.
        conn.execute('INSERT OR REPLACE INTO verification_metadata_fetch VALUES (?,?)',(meta,now+120))
    _,kind,imdb=meta.split(':')
    worker='metadata-dependency-'+uuid.uuid4().hex
    jobs.enqueue([(kind,imdb,2000)])
    try:lease=jobs.claim('server',worker,kind,imdb)
    except MetadataBusy:return None
    if lease is None:
        with F.db() as conn:conn.execute('UPDATE verification_metadata_fetch SET next_fetch=? WHERE metadata_key=? AND next_fetch=?',(now+3,meta,now+120))
        return False
    if metadata_pool_config()['enabled']:
        with jobs.db(True) as provider:
            burst=jobs.value(provider,'preparation_burst')+1
            if burst>=4:
                # Leave a normal provider slot for catalogue-page callers.
                jobs.put(provider,'preparation_yield_until',time.time()+6)
                burst=0
            jobs.put(provider,'preparation_burst',burst)
    previous=getattr(BUDGET,'metadata_background',False)
    BUDGET.metadata_background=True
    stage='provider_fetch'
    try:
        response=fetch(f'{CINEMETA}/meta/{kind}/{imdb}.json',timeout=8)
        stage='provider_envelope'
        if not isinstance(response,dict):raise MetadataResponseShapeError('invalid_meta')
        if 'meta' not in response:raise MetadataResponseShapeError('missing_meta')
        data=response['meta']
        if not isinstance(data,dict):raise MetadataResponseShapeError('invalid_meta')
        id_ok=data.get('id')==imdb
        type_ok=data.get('type')==kind
        name=data.get('name')
        name_ok=isinstance(name,str) and bool(name.strip())
        if not id_ok or not type_ok or not name_ok:
            if not id_ok and not type_ok:code='id_type_mismatch'
            elif not id_ok:code='id_mismatch'
            elif not type_ok:code='type_mismatch'
            elif name is None or not isinstance(name,str):code='missing_name'
            else:code='invalid_name'
            raise MetadataResponseShapeError(code)
        stage='coordinator_receipt'
        ack=jobs.receive('server',worker,lease,{'result_id':uuid.uuid4().hex,
            'source':'cinemeta','fetched_at':time.time(),'status':'ok','metadata':data})
        return ack in ('accepted','duplicate')
    except Exception as error:
        status=getattr(error,'code',None)
        record_metadata_retry(error,stage)
        retry_delay=3600 if isinstance(error,MetadataResponseShapeError) else (900 if status==429 else 300)
        try:
            ack=jobs.receive('server',worker,lease,{'result_id':uuid.uuid4().hex,
                'source':'cinemeta','fetched_at':time.time(),'status':'retry',
                'provider_status':status,'retry_after_seconds':retry_delay})
        except Exception as receipt_error:
            record_metadata_retry(receipt_error,'retry_receipt')
            raise
        if ack in ('accepted','duplicate'):
            with F.db() as conn:
                conn.execute('UPDATE verification_metadata_fetch SET next_fetch=? WHERE metadata_key=?',
                             (time.time()+retry_delay,meta))
        return False
    finally:BUDGET.metadata_background=previous


def metadata_import(limit=20,key=None,seed=False):
    """Apply durable metadata handoffs. No source proofs/native userdata writes."""
    jobs=metadata_coordinator(DATA)
    if not jobs:return 0
    applied=0
    if seed and not metadata_pool_config()['enabled']:
        # Bound the run below the service timeout; each claim still reserves
        # the shared provider slot. Import before selecting another parent.
        for position in range(4):
            if position:time.sleep(3.1)
            fetched=fetch_waiting_metadata(jobs)
            applied+=metadata_import(limit=limit)
            if fetched is None:break
            if not fetched:
                provider=jobs.status()
                if not provider.get('enabled') or provider.get('provider_after',0)>time.time():break
    if seed:
        jobs.seed(DATA/'catalogue.sqlite')
        with F.db() as conn:conn.execute('DELETE FROM metadata_waits WHERE expires<?',(time.time()-3600,))
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
            parent=kind+':'+imdb
            has_preparation=bool(conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='preparation_work'").fetchone())
            if not has_preparation or not conn.execute('SELECT 1 FROM preparation_work WHERE key=?',(parent,)).fetchone():
                conn.execute('DELETE FROM metadata_waits WHERE key=? AND NOT EXISTS(SELECT 1 FROM verification_retry_waits WHERE key=?)',(parent,parent))
            conn.execute('DELETE FROM metadata_preparation_requests WHERE metadata_key=?',('meta:'+parent,))
            conn.execute("UPDATE metadata_preparation_control SET value=0 WHERE key='checked'")
            F.resume_metadata_dependencies(conn,'meta:'+kind+':'+imdb)
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
    label='HE' if 'heb' in langs else 'EN' if 'eng' in langs else '?'
    return label+('+RU' if (kind,imdb) in RUSSIAN.badge_titles({(kind,imdb)}) else '')


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
    russian=RUSSIAN.badge_titles(languages)
    return {key:('HE' if 'heb' in langs else 'EN' if 'eng' in langs else '?')
            +('+RU' if key in russian else '') for key,langs in languages.items()}


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
    subtitle_badge=badge.split('+',1)[0]
    draw.rounded_rectangle((8,top,83,top+40),radius=6,fill={'HE':'#126b47','EN':'#b3261e','?':'#555555'}[subtitle_badge])
    draw.text((22,top+4),subtitle_badge,font=font,fill='white')
    if '+RU' in badge:
        left=max(8,image.width-83);bottom=max(8,image.height-48)
        draw.rounded_rectangle((left,bottom,left+75,bottom+40),radius=6,fill='#753bbd')
        draw.text((left+14,bottom+4),'RU',font=font,fill='white')
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
    lock_path=DATA/'nfo-metadata.lock';lock_path.parent.mkdir(parents=True,exist_ok=True)
    with lock_path.open('a+') as lock:
        fcntl.flock(lock,fcntl.LOCK_EX)
        temporary=path.with_suffix(path.suffix+'.new')
        ET.ElementTree(root).write(temporary,encoding='utf-8',xml_declaration=True)
        temporary.replace(path)


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
        values={'title':name,'originaltitle':meta.get('englishName') or meta.get('originalName'),
                'year':year,'imdbid':imdb,'plot':meta.get('description'),
                'premiered':str(meta.get('released',''))[:10], 'rating':meta.get('imdbRating'),
                'runtime':re.sub(r'\D','',str(meta.get('runtime',''))) or None,'trailer':trailer_urls(meta)}
        nfo(folder/'movie.nfo','movie',values,meta.get('genres'))
    else:
        nfo(folder/'tvshow.nfo','tvshow',{'title':name,'originaltitle':meta.get('englishName') or meta.get('originalName'),
            'year':year,'imdbid':imdb,'plot':meta.get('description'),
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


def mediafusion_streams(key,priority=0):
    marker=DATA/'source-providers.json'
    if not marker.exists():return None
    cfg=json.loads(marker.read_text());base=cfg.get('mediafusion_url','').rstrip('/')
    if not cfg.get('mediafusion_enabled'):return None
    parsed=U.urlsplit(base)
    if parsed.scheme!='https' or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise Checkpoint('Invalid fallback provider configuration')
    saved=cached('mediafusion:'+key)
    if saved is not None:return saved
    slots=cfg.get('mediafusion_queue_slots')
    if slots is not None and (type(slots) is not int or not 1<=slots<=32):raise Checkpoint('Invalid fallback queue capacity')
    with serialized_provider_slot(DATA/'mediafusion-provider.lock','mediafusion-provider-control',priority=priority,
                                  queue_slots=slots,queue_spacing_seconds=15,
                                  wait_seconds=30 if cfg.get('jackett_enabled') else None):
        kind,imdb,*parts=key.split(':');ident=imdb+(':'+':'.join(parts) if parts else '')
        try:
            result=fetch(base+'/stream/'+kind+'/'+ident+'.json',timeout=15)
            if not isinstance(result,dict) or not isinstance(result.get('streams'),list):raise ValueError('Invalid fallback source response')
            raw_stats=mediafusion_origin_counts(result['streams'])
            remember('mediafusion-raw-counts:'+key,raw_stats,1800)
            rows=[]
            for row in result['streams']:
                if not isinstance(row,dict):continue
                description=str(row.get('description') or row.get('title') or '')
                origin=re.search(r'🔗\s*([^\n]+)',description)
                if not origin or 'torrentio' in origin[1].casefold():continue
                filename=str((row.get('behaviorHints') or {}).get('filename') or '')
                candidate={**row,'title':filename+'\n'+description,'_provider':'mediafusion'}
                if 'sources' in row:candidate['sources']=tracker_sources(row['sources'])
                rows.append(candidate)
            provider_succeeded('mediafusion-provider-control',15)
            return remember('mediafusion:'+key,rows,1800 if rows else 900)
        except urllib.error.HTTPError as error:
            provider_failed('mediafusion-provider-control',error,15)
            failure=Checkpoint('Fallback source provider unavailable')
            failure.provider_failure_category=source_provider_error_category(error)
            raise failure from None
        except (urllib.error.URLError,TimeoutError,ValueError) as error:
            provider_failed('mediafusion-provider-control',error,15)
            failure=Checkpoint('Fallback source provider unavailable')
            failure.provider_failure_category=source_provider_error_category(error)
            raise failure from None


def source_provider_config():
    marker=DATA/'source-providers.json'
    return json.loads(marker.read_text()) if marker.exists() else {}


def source_config_signature():
    cfg=source_provider_config()
    selected={k:cfg.get(k) for k in ('mediafusion_enabled','mediafusion_url','jackett_enabled','jackett_url','jackett_indexers')}
    return hashlib.sha256(json.dumps(selected,sort_keys=True).encode()).hexdigest()[:20]


def jackett_cache_key(key,cfg=None):
    cfg=source_provider_config() if cfg is None else cfg
    indexers=cfg.get('jackett_indexers',{}).get(key.split(':')[0],[])
    if len(indexers)<=1:return 'jackett:v2:'+key
    signature=hashlib.sha256(json.dumps(indexers).encode()).hexdigest()[:16]
    return 'jackett:v3:'+signature+':'+key


def jackett_indexer_cache_key(indexer,key):
    return 'jackett-indexer:v1:'+indexer+':'+key


def cached_expiry(key):
    with db() as conn:row=conn.execute('SELECT expires FROM cache WHERE key=? AND expires>?',(key,time.time())).fetchone()
    return row[0] if row else 0


def source_recovery_wait(key,error=None):
    now=time.time();cfg=source_provider_config();deadlines=[];providers=[]
    primary=cached('source-provider-control') or {}
    retry=getattr(error,'retry_after',None)
    if not isinstance(retry,(int,float)) or retry<=0:
        retry=900 if getattr(error,'code',None)==429 else 60
    deadlines.append(max(now+1,primary.get('cooldown',0),cached_expiry('torrentio-empty:'+key))
                     if primary.get('cooldown',0)>now or cached('torrentio-empty:'+key) else now+retry)
    providers.append('torrentio')
    if cfg.get('mediafusion_enabled'):
        control=cached('mediafusion-provider-control') or {}
        expiry=cached_expiry('mediafusion:'+key) if cached('mediafusion:'+key)==[] else 0
        deadlines.append(max(now+1,control.get('cooldown',0),expiry) if control.get('cooldown',0)>now or expiry else now+60)
        providers.append('mediafusion')
    if cfg.get('jackett_enabled') and cfg.get('jackett_indexers',{}).get(key.split(':')[0]):
        expiry=cached_expiry(jackett_cache_key(key,cfg)) if cached(jackett_cache_key(key,cfg))==[] else 0
        if not expiry:
            ready=[]
            for indexer in cfg['jackett_indexers'][key.split(':')[0]]:
                control=cached('jackett-'+indexer+'-control') or {}
                item=jackett_indexer_cache_key(indexer,key)
                negative=cached_expiry(item) if cached(item)==[] else 0
                ready.append(max(now+1,control.get('cooldown',0),negative) if negative or control.get('cooldown',0)>now else now+30)
            expiry=min(ready)
        deadlines.append(expiry);providers.append('jackett')
    retry_at=max(now+1,min(deadlines))
    remember('source-recovery:'+key,{'retry_at':retry_at,'signature':source_config_signature(),
             'primary_cooldown':primary.get('cooldown',0),'providers':providers},retry_at-now)
    wait=SourceProviderWait('Source providers awaiting recovery','mixed','recovery',retry_at-now)
    wait.providers=providers
    return wait


def retained_source_recovery(key):
    state=cached('source-recovery:'+key)
    if not state or state.get('signature')!=source_config_signature():return None
    current=cached('source-provider-control') or {}
    if state.get('primary_cooldown',0)>0 and current.get('cooldown',0)<state['primary_cooldown']-1:return None
    # A successful concurrent lookup or newly enabled indexer must not be
    # blocked by an older negative/recovery record.
    if cached('mediafusion:'+key) or cached(jackett_cache_key(key)):return None
    retry=state.get('retry_at',0)-time.time()
    if retry<=0:return None
    wait=SourceProviderWait('Source providers awaiting recovery','mixed','recovery',retry)
    wait.providers=state.get('providers',[])
    return wait


def jackett_streams(key,priority=0):
    marker=DATA/'source-providers.json'
    cfg=json.loads(marker.read_text()) if marker.exists() else {}
    if not cfg.get('jackett_enabled'):return None
    base=cfg.get('jackett_url','').rstrip('/')
    # The only supported deployment is the private Compose service, not a
    # caller-supplied URL or a link returned by an indexer.
    if base!='http://jackett:9117':raise Checkpoint('Invalid local indexer configuration')
    kind,imdb,*episode=key.split(':')
    indexers=cfg.get('jackett_indexers',{}).get(kind,[])
    if not isinstance(indexers,list) or len(indexers)>3 or any(not re.fullmatch('[a-z0-9-]{1,40}',i) for i in indexers):
        raise Checkpoint('Invalid local indexer list')
    if not indexers:return None
    listing_key=jackett_cache_key(key,cfg)
    saved=cached(listing_key)
    if saved is not None:return saved
    try:token=(DATA/'jackett-api-key').read_text().strip()
    except OSError:raise Checkpoint('Local indexer credential unavailable') from None
    if not re.fullmatch('[a-zA-Z0-9]{32,128}',token):raise Checkpoint('Invalid local indexer credential')
    # Use only existing local metadata; discovery must not start another
    # metadata-provider lookup inside the indexer budget.
    meta=F.local_index_metadata(kind,imdb) or cached('meta:'+kind+':'+imdb) or {}
    if not meta:
        with db() as conn:row=conn.execute('SELECT meta FROM items WHERE key=?',(kind+':'+imdb,)).fetchone()
        if row:
            try:meta=json.loads(row[0])
            except (ValueError,TypeError):pass
    combined=[];failure=None;completed_expiries=[]
    for indexer in indexers:
        completed_key=jackett_indexer_cache_key(indexer,key)
        previous=cached(completed_key)
        if previous is not None:
            completed_expiries.append(cached_expiry(completed_key))
            source_provider_event('jackett','compatible' if previous else 'empty','hit',len(previous))
            if previous:
                combined.extend(previous);break
            continue
        control='jackett-'+indexer+'-control'
        try:
            with serialized_provider_slot(DATA/('jackett-'+indexer+'.lock'),control,priority=priority,
                                          queue_slots=2,queue_spacing_seconds=15):
                try:
                    endpoint=base+'/api/v2.0/indexers/'+indexer+'/results/torznab/api'
                    caps=cached('jackett-caps:'+indexer)
                    if caps is None:
                        raw=fetch(endpoint+'?'+U.urlencode({'apikey':token,'t':'caps'}),raw=True,timeout=10)
                        # Parse before caching, including provider-level XML errors.
                        search_parameters(raw,key,meta)
                        caps=remember('jackett-caps:'+indexer,raw.decode('utf-8'),86400)
                    query=search_parameters(caps.encode('utf-8') if isinstance(caps,str) else caps,key,meta)
                    query['apikey']=token
                    raw=fetch(endpoint+'?'+U.urlencode(query),raw=True,timeout=15)
                    raw_stats={}
                    rows=torznab_streams(raw,key,raw_stats)
                    provider_succeeded(control,15)
                    clean,stats=filter_streams_with_diagnostics({'streams':rows})
                    if kind=='series':
                        names=[meta.get(n,'') for n in ('name','original_title','originalName')]
                        with F.db() as conn:
                            alias=conn.execute('SELECT english FROM aliases WHERE imdb=?',(imdb,)).fetchone()
                            local=conn.execute("SELECT name,original FROM home_titles WHERE imdb=? AND kind='series'",(imdb,)).fetchone()
                        if alias:names.append(alias[0])
                        if local:names.extend(local)
                        matched=[r for r in clean if series_title_matches(r.get('behaviorHints',{}).get('filename',''),key,names,str(meta.get('year') or meta.get('releaseInfo') or '')[:4])]
                        raw_stats['identity_rejected']+=len(clean)-len(matched);clean=matched;stats['compatible']=len(clean)
                    # Persist each successful listing before advancing to a
                    # different indexer's queue. A later admission wait must
                    # not force this completed search to run again.
                    remember(completed_key,clean[:20],1800 if clean else 900)
                    completed_expiries.append(cached_expiry(completed_key))
                    source_provider_event('jackett','compatible' if clean else 'empty',provider_rows=len(rows),stats=stats,raw_stats=raw_stats)
                    state=cached(control) or {};state.update(raw_rows=raw_stats['raw_rows'],provider_rows=len(rows),compatible=len(clean),identity_rejected=raw_stats['identity_rejected'])
                    remember(control,state,90000)
                    combined.extend(clean)
                    if clean:break
                except (urllib.error.URLError,TimeoutError,ValueError,ET.ParseError) as error:
                    if isinstance(error,urllib.error.HTTPError):
                        # Jackett can wrap an upstream reset/429 as HTTP400.
                        # Bound inspection; the adapter status still owns backoff.
                        try:
                            response=getattr(error,'fp',None)
                            sock=getattr(getattr(getattr(response,'fp',None),'raw',None),'_sock',None)
                            if sock is not None:sock.settimeout(remaining(1))
                            body=error.read(8193)
                        except Exception:body=None
                        finally:
                            try:error.close()
                            except Exception:pass
                        error.indexer_details=indexer_error_details(error.code,body)
                    provider_failed(control,error,15)
                    raise
        except (Checkpoint,urllib.error.URLError,TimeoutError,ValueError,ET.ParseError) as error:
            source_provider_event('jackett','error',error=error)
            failure=error
    if not combined and failure:
        if isinstance(failure,SourceProviderWait) and failure.reason not in ('cooldown','recovery'):raise failure
        raise source_recovery_wait(key,failure) from None
    combined=filter_streams_with_diagnostics({'streams':combined})[0]
    # The compound listing must not extend an older component's freshness.
    ttl=max(1,min(completed_expiries)-time.time())
    return remember(listing_key,combined[:20],ttl)


def fallback_candidates(key,priority=0):
    failure=None;fallback=None
    cache='hit' if cached('mediafusion:'+key) is not None else 'miss'
    try:
        fallback=mediafusion_streams(key,priority=priority)
        if fallback is not None:
            choices,stats=filter_streams_with_diagnostics({'streams':fallback})
            source_provider_event('mediafusion','compatible' if choices else 'empty',cache,len(fallback),stats,
                                  raw_stats=cached('mediafusion-raw-counts:'+key))
            if choices:return choices
    except (Checkpoint,urllib.error.URLError,TimeoutError,ValueError) as error:
        source_provider_event('mediafusion','error',cache,error=error)
        failure=error
    local=jackett_streams(key,priority=priority)
    if local:return local
    # An unavailable provider is not a genuine empty listing, even when
    # another provider searched successfully but had no candidates.
    if failure:
        if isinstance(failure,SourceProviderWait):raise failure
        raise source_recovery_wait(key,failure) from None
    return [] if fallback is not None or local is not None else None


def mediafusion_origin_counts(streams):
    """Return privacy-safe aggregate counts before fallback origin filtering."""
    counts={'raw_rows':len(streams),'malformed_rows':0,'torrentio_origin_rows':0,
            'named_origin_rows':0,'missing_or_unknown_origin_rows':0}
    for row in streams:
        if not isinstance(row,dict):
            counts['malformed_rows']+=1
            continue
        description=str(row.get('description') or row.get('title') or '')
        origin=re.search(r'🔗\s*([^\n]+)',description)
        if not origin or not origin[1].strip():
            counts['missing_or_unknown_origin_rows']+=1
        elif 'torrentio' in origin[1].casefold():
            counts['torrentio_origin_rows']+=1
        else:
            counts['named_origin_rows']+=1
    return counts
def filtered_streams(response):
    return _filter_streams(response)


def filter_streams_with_diagnostics(response):
    """Apply the unchanged compatibility filter and return safe reason counts."""
    stats={}
    candidates=_filter_streams(response,stats)
    return candidates,stats


def _filter_streams(response,stats=None):
    streams=response.get('streams',[])
    if stats is not None:
        stats.update({'provider_rows':len(streams),'invalid_infohash':0,'unsupported_release':0,
                      'unsupported_codec':0,'unsupported_resolution':0,'unsupported_container':0,
                      'duplicate_infohash':0,'compatible':0})
    candidates=[]
    for stream in streams:
        infohash=stream.get('infoHash','')
        if not re.fullmatch('[a-fA-F0-9]{40}',infohash):
            if stats is not None:stats['invalid_infohash']+=1
            continue
        title=stream.get('title','')+' '+stream.get('name','')
        if re.search(r'2160|4K|CAM\b|\bTS\b|HDCAM|TELESYNC|AV1|10[ ._-]?bit|Main[ ._-]?10|HDR|Dolby[ ._-]?Vision|\bDV\b',title,re.I):
            if stats is not None:
                stats['unsupported_codec' if re.search(r'AV1|10[ ._-]?bit|Main[ ._-]?10|HDR|Dolby[ ._-]?Vision|\bDV\b',title,re.I) else 'unsupported_release']+=1
            continue
        if not re.search('1080|720',title,re.I):
            if stats is not None:stats['unsupported_resolution']+=1
            continue
        candidate=dict(stream)
        hinted_filename=stream.get('behaviorHints',{}).get('filename','')
        if Path(hinted_filename).suffix.lower() in ('.iso','.rar','.zip'):
            if stats is not None:stats['unsupported_container']+=1
            continue
        seeds=re.search(r'(?:👤|seeders?[: ]*)\s*(\d+)',title,re.I)
        # Counts are hints; even a reported zero can be stale.
        candidate['_score']=(2000 if '1080' in title else 1000)+(500 if re.search('x264|h[ .]?264',title,re.I) else 0)+(200 if 'AAC' in title.upper() else 0)+min(int(seeds[1]) if seeds else 0,3000)*3
        candidates.append(candidate)
    seen=set();unique=[]
    for row in candidates:
        h=row['infoHash'].lower()
        if h in seen:
            if stats is not None:stats['duplicate_infohash']+=1
            continue
        seen.add(h);row['infoHash']=h;unique.append(row)
    if stats is not None:stats['compatible']=len(unique)
    return unique


def source_provider_error_category(error):
    """Stable provider-failure label; never expose URL, title, or exception text."""
    saved=getattr(error,'provider_failure_category',None)
    if isinstance(saved,str) and re.fullmatch(r'(?:network|timeout|invalid_response|http_[1-5][0-9]{2})',saved):return saved
    if isinstance(error,SourceProviderWait):
        return 'wait_'+(error.reason if error.reason in ('busy','queue_timeout','cooldown','pacing','capacity','recovery') else 'other')
    if isinstance(error,urllib.error.HTTPError):
        try:code=int(error.code)
        except (TypeError,ValueError):code=0
        return 'http_'+str(code) if 100<=code<=599 else 'http_other'
    if isinstance(error,(TimeoutError,socket.timeout)):return 'timeout'
    if isinstance(error,urllib.error.URLError):return 'network'
    if isinstance(error,(ValueError,ET.ParseError)):return 'invalid_response'
    if isinstance(error,Checkpoint):return 'checkpoint'
    return 'other'


def source_provider_event(provider,outcome,cache='miss',provider_rows=None,stats=None,error=None,primary_cache=None,raw_stats=None):
    """Emit low-cardinality lookup telemetry without title/source identifiers."""
    event={'provider':provider,'outcome':outcome,'cache':cache}
    if provider_rows is not None:event['provider_rows']=max(0,int(provider_rows))
    if stats is not None:
        for name in ('invalid_infohash','unsupported_release','unsupported_codec','unsupported_resolution',
                     'unsupported_container','duplicate_infohash','compatible'):
            event[name]=int(stats.get(name,0))
    if raw_stats is not None:
        for name in ('raw_rows','malformed_rows','torrentio_origin_rows','named_origin_rows','missing_or_unknown_origin_rows','identity_rejected'):
            event['raw_'+name if name!='raw_rows' else name]=max(0,int(raw_stats.get(name,0)))
    if error is not None:
        event['error']=source_provider_error_category(error)
        if provider=='jackett':
            details=getattr(error,'indexer_details',{})
            if isinstance(details,dict):
                if type(details.get('adapter_http_status')) is int and 100<=details['adapter_http_status']<=599:event['adapter_http_status']=details['adapter_http_status']
                if details.get('indexer_failure') in ('unknown','connection_reset','upstream_http_429'):event['indexer_failure']=details['indexer_failure']
                if details.get('upstream_http_status')==429:event['upstream_http_status']=429
    if primary_cache is not None:event['primary_cache']=primary_cache
    try:
        if cache=='miss' and outcome in ('compatible','empty') and provider in ('torrentio','mediafusion','jackett'):
            remember('provider-observation:'+provider,{'updated':time.time(),**event},90000)
        log('Source discovery '+json.dumps(event,sort_keys=True,separators=(',',':')))
    except Exception:pass # Diagnostics must never change source selection.


def request_mediafusion_after_slow_primary(key,selected,reason,background):
    """Queue a fallback lookup only after background V6 rejects Torrentio speed."""
    if (not background or reason!='speed_rejected' or not isinstance(selected,dict)
        or selected.get('local') or selected.get('source_provider')!='torrentio'):
        return False
    source_hash=selected.get('hash')
    marker={'rejected_hash':source_hash.lower()} if isinstance(source_hash,str) and re.fullmatch(r'[a-f0-9]{40}',source_hash.lower()) else True
    remember('mediafusion-needed:'+key,marker,21600)
    return True


def add_mediafusion_after_slow_primary(key,saved):
    """Merge paced fallback candidates after a proven slow primary source."""
    marker='mediafusion-needed:'+key
    if not getattr(BUDGET,'validation_background',False) or not cached(marker):return saved
    choices=fallback_candidates(key,priority=1)
    if choices is None:return saved
    combined=list(saved);seen={candidate.get('infoHash','').lower() for candidate in combined}
    for candidate in choices:
        infohash=candidate['infoHash'].lower()
        BUDGET.diagnostic_source_hash=infohash
        if infohash not in seen:
            seen.add(infohash);combined.append(candidate)
    combined.sort(key=rank,reverse=True)
    remember(marker,False,0)
    return remember('streams:'+key,combined,1800)


def stream_candidates(key):
    # Stable, publisher-distributed open-film source for setup/playback checks.
    # Preserve HTTP web seeds by adding its actual .torrent rather than a magnet.
    open_film={'infoHash':'dd8255ecdc7ca55fb0bbf81323d87062db1f6d1c',
        'torrentUrl':'https://webtorrent.io/torrents/big-buck-bunny.torrent',
        'title':'Big Buck Bunny — publisher web-seeded release',
        'behaviorHints':{'filename':'Big Buck Bunny.mp4'},'_score':999999,'_provider':'other'}
    saved=cached('streams:'+key)
    if saved and key.startswith('movie:'):
        clean=[r for r in saved if r.get('_provider')!='jackett' or r.get('_source_imdb')==key.split(':')[1]]
        if len(clean)!=len(saved):saved=clean or None
    if not saved:
        recovery=retained_source_recovery(key)
        if recovery:raise recovery
    if saved==[]:
        choices=fallback_candidates(key)
        if choices:
            saved=remember('streams:'+key,choices[:20],1800)
            if not getattr(BUDGET,'speculative',False):
                remember('source-status:'+key,{'state':'listed','checked_at':time.time()},21600)
    if saved is not None:
        # Rows cached before provider attribution were only produced by the
        # primary Torrentio path; preserve explicit MediaFusion labels.
        for row in saved:
            if isinstance(row,dict):row.setdefault('_provider','torrentio')
        saved=add_mediafusion_after_slow_primary(key,saved)
        if saved:
            providers=sorted({row.get('_provider','torrentio') for row in saved if isinstance(row,dict)})
            providers=[p if p in ('torrentio','mediafusion','jackett','other') else 'other' for p in providers]
            source_provider_event('+'.join(sorted(set(providers))) or 'unknown','compatible','hit',len(saved))
        if not getattr(BUDGET,'speculative',False) and not cached('source-status:'+key):
            remember('source-status:'+key,{'state':'listed' if saved else 'unavailable','checked_at':time.time()},21600)
        if key=='movie:tt1254207' and not cached('open-film-failed') and not any(s['infoHash']==open_film['infoHash'] for s in saved):
            return [open_film]+saved
        return sorted(saved,key=rank,reverse=True)
    kind,imdb,*parts=key.split(':')
    ident=imdb+(':'+':'.join(parts) if parts else '')
    primary_error=None
    try:
        response=fetch(f'{PROVIDER}/stream/{kind}/{ident}.json',timeout=15)
        if not isinstance(response,dict) or not isinstance(response.get('streams'),list):raise ValueError('Invalid source response')
        candidates,stats=filter_streams_with_diagnostics(response)
        source_provider_event('torrentio','compatible' if candidates else 'empty','miss',len(response['streams']),stats)
        if not candidates and primary_error is None:
            # If the fallback provider is locally queued, retain this short
            # primary-negative result. Otherwise every retry repeats the same
            # paced Torrentio lookup before rejoining the fallback queue.
            # A completed fallback result replaces this with its normal TTL.
            remember('streams:'+key,[],300)
            remember('torrentio-empty:'+key,True,300)
    except (SourceProviderWait,urllib.error.URLError,TimeoutError,ValueError) as error:
        primary_error=error;response={'streams':[]};candidates=[]
        source_provider_event('torrentio','error','miss',error=error)
    for candidate in candidates:candidate['_provider']='torrentio'
    # A speed-rejected source may have expired from the ordinary listing
    # cache before its scheduled retry. In that cache-miss path, still consult
    # the paced fallback when Torrentio returns candidates; otherwise a fresh
    # primary listing would silently bypass the retry marker.
    if candidates and getattr(BUDGET,'validation_background',False) and cached('mediafusion-needed:'+key):
        candidates=add_mediafusion_after_slow_primary(key,candidates)
    if not candidates:
        fallback=fallback_candidates(key)
        if fallback is not None:candidates=fallback
        if not candidates and primary_error:raise source_recovery_wait(key,primary_error) from None
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
    independent=(candidate.get('_provider')=='jackett' or candidate.get('_discovery_provider')=='jackett')
    if key.startswith('movie:') and independent and candidate.get('_source_imdb')!=key.split(':')[1]:return False
    if key.startswith('series:'):
        _,imdb,season,episode=key.split(':')
        if candidate.get('_provider')=='jackett' or candidate.get('_discovery_provider')=='jackett':
            meta=F.local_index_metadata('series',imdb) or cached('meta:series:'+imdb) or {}
            if not meta:
                with db() as conn:row=conn.execute('SELECT meta FROM items WHERE key=?',('series:'+imdb,)).fetchone()
                if row:meta=json.loads(row[0])
            names=[meta.get(n,'') for n in ('name','original_title','originalName')]
            with F.db() as conn:
                alias=conn.execute('SELECT english FROM aliases WHERE imdb=?',(imdb,)).fetchone()
                local=conn.execute("SELECT name,original FROM home_titles WHERE imdb=? AND kind='series'",(imdb,)).fetchone()
            if alias:names.append(alias[0])
            if local:names.extend(local)
            return series_title_matches(title,key,names,str(meta.get('year') or meta.get('releaseInfo') or '')[:4])
        return bool(re.search(rf'(?:S0*{int(season)}[ ._-]*E0*{int(episode)}(?!\d)|(?<!\d)0*{int(season)}x0*{int(episode)}(?!\d))',title,re.I)) or candidate.get('fileIdx') is not None
    with db() as conn:row=conn.execute('SELECT meta FROM items WHERE key=?',(key,)).fetchone()
    if not row:
        # Resolve the provider identity once, never guess a movie from a release.
        imdb=key.split(':')[1]
        meta=F.local_index_metadata('movie',imdb) if getattr(BUDGET,'validation_background',False) else None
        if meta is None:meta=metadata('movie',imdb)
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


def verification_candidates(key):
    seen=set()
    for selected in REL.choices(key):
        if selected.get('local'):continue
        seen.add(selected['hash'])
        provider=selected.get('source_provider') or ('other' if selected['hash']=='dd8255ecdc7ca55fb0bbf81323d87062db1f6d1c' else 'torrentio')
        yield {'infoHash':selected['hash'],'torrentUrl':selected.get('torrent_url'),'_exact':selected,'_provider':provider}
    selected=cached('resolved:'+key)
    if selected and selected.get('hash') not in seen and not REL.excluded(key,selected):
        seen.add(selected['hash']);provider=selected.get('source_provider') or ('other' if selected['hash']=='dd8255ecdc7ca55fb0bbf81323d87062db1f6d1c' else 'torrentio')
        yield {'infoHash':selected['hash'],'torrentUrl':selected.get('torrent_url'),'_exact':selected,'_provider':provider}
    for candidate in stream_candidates(key):
        if candidate['infoHash'].lower() not in seen and candidate_matches(candidate,key):yield candidate

def resolve(key,deadline=None):
    local=F.local_download(key)
    if local:return local
    background=getattr(BUDGET,'validation_background',False)
    metadata_pending=bool(cached('metadata-pending:'+key))
    deferred_metadata=0
    # Persisted sources precede discovery, including previously verified
    # alternatives. A healthy preferred source never triggers a provider search.
    for candidate in verification_candidates(key):
        remaining(1)
        source_provider=candidate.get('_provider','torrentio')
        if source_provider not in ('torrentio','mediafusion','jackett','other'):source_provider='none'
        BUDGET.remote_provider=source_provider
        if background and getattr(BUDGET,'remote_verification',False):
            from verification_remote import producer_phase
            producer_phase(globals(),key,'torrent_resolution',source_provider)
        infohash=candidate['infoHash'].lower()
        if cached('source-excluded:'+key+':'+infohash):continue
        exact=candidate.get('_exact')
        trackers=['udp://tracker.opentrackr.org:1337/announce']
        trackers += [s[len('tracker:'):] for s in candidate.get('sources',[]) if s.startswith('tracker:')]
        magnet='magnet:?xt=urn:btih:'+infohash+''.join('&tr='+U.quote(t,safe='') for t in trackers)
        try:
            metadata_attempt=time.monotonic()
            status={}
            work=REL.checkpoint(key)
            metadata_started=work.get('metadata_started',time.time()) if background and work.get('metadata_hash')==infohash else time.time()
            metadata_age=max(0,time.time()-metadata_started)
            # A background producer should not occupy a slot for the full
            # metadata timeout. Yield after 45s, retain the title and
            # source for retry, then continue with another candidate/title.
            metadata_window=min(METADATA_SECONDS,45) if background else METADATA_SECONDS
            end=min(time.monotonic()+max(0,min(metadata_window,METADATA_SECONDS-metadata_age)),deadline or float('inf'))
            REL.checkpoint(key,phase='metadata',state='preparing',source_hash=infohash,metadata_hash=infohash,metadata_started=metadata_started)
            try:status=torr({'action':'get','hash':infohash},timeout=2)
            except (ClientCancelled,PlaybackBusy,Checkpoint):raise
            except Exception:pass
            if not status.get('file_stats'):
                try:
                    record_background_torrent(key,infohash)
                    status=torr({'action':'add','link':(exact or {}).get('link') or candidate.get('torrentUrl') or magnet,'save_to_db':False},timeout=min(2,remaining(2)))
                except (ClientCancelled,PlaybackBusy,Checkpoint):raise
                except Exception:pass
            while not status.get('file_stats') and time.monotonic()<end:
                remaining(1)
                try:status=torr({'action':'get','hash':infohash},timeout=min(2,remaining(2)))
                except (ClientCancelled,PlaybackBusy,Checkpoint):raise
                except Exception:pass
                if not status.get('file_stats'):time.sleep(min(2,remaining(2)))
            if not status.get('file_stats'):
                if deadline and time.monotonic()>=deadline:raise Checkpoint('Metadata checkpoint','preparation_budget')
                if background and time.time()-metadata_started<METADATA_SECONDS:
                    metadata_pending=True;deferred_metadata+=1
                    remember('metadata-pending:'+key,True,900)
                    remember('source-excluded:'+key+':'+infohash,True,300)
                    REL.checkpoint(key,phase='metadata_pending',state='checkpoint',metadata_hash=infohash,
                                   metadata_started=metadata_started,metadata_pending=True)
                    if deferred_metadata>=2:
                        from verification_remote import producer_phase
                        producer_phase(globals(),key,'waiting for source metadata')
                        raise Checkpoint('Torrent metadata pending; retry other titles','metadata_pending')
                    continue
                raise SourceStalled('Metadata unavailable')
            if status.get('hash',infohash).lower()!=infohash:raise diagnostic_tag(SourceFailure('Torrent identity changed'),'source_identity_mismatch')
            if exact:
                matches=[f for f in status['file_stats'] if f.get('id')==exact['index'] and f.get('path')==exact['filename'] and f.get('length')==exact['length']]
                if len(matches)!=1:raise diagnostic_tag(SourceFailure('Exact source file changed'),'source_file_changed')
                selected={**exact,'source_provider':source_provider}
            else:
                try:f=choose_file(status['file_stats'],key,candidate.get('fileIdx'),candidate.get('behaviorHints',{}).get('filename'))
                except ValueError:raise diagnostic_tag(SourceFailure('Source file selection rejected'),'source_file_unmatched') from None
                if key.startswith('movie:') and not candidate_matches({**candidate,'behaviorHints':{'filename':f['path']}},key):raise diagnostic_tag(SourceFailure('Movie filename does not match requested title/year'),'source_title_mismatch')
                selected={'hash':infohash,'index':f['id'],'filename':f['path'],'length':f['length'],
                    'source_name':candidate.get('title',''),'file_count':len(status['file_stats']),'torrent_url':candidate.get('torrentUrl'),'link':candidate.get('torrentUrl') or magnet,
                    'source_provider':source_provider}
            if REL.excluded(key,selected):continue
            playback_event(key,'torrent_metadata_ready',metadata_attempt,'ok')
            # Native persistence allows later get/add to recover InfoBytes
            # locally, while exact hash/file validation still applies.
            if exact and not background:retain_torrent_metadata(selected,status)
            remember('metadata-pending:'+key,False,0)
            REL.bind(key,selected)
            return remember('resolved:'+key,selected,900)
        except (ClientCancelled,PlaybackBusy,Checkpoint):raise
        except Exception as error:
            record_source_failure(BUDGET,error,'torrent resolution',infohash)
            remaining(1)
            if deadline and deadline-time.monotonic()<.5:raise Checkpoint('Source checkpoint') from None
            if getattr(BUDGET,'speculative',False):raise
            if not isinstance(error,SourceFailure):raise
            if exact:REL.exclude(key,exact)
            remember('source-excluded:'+key+':'+infohash,True,900)
            log('Trying next source for '+key)
    if metadata_pending:
        from verification_remote import producer_phase
        producer_phase(globals(),key,'waiting for source metadata')
        raise Checkpoint('Torrent metadata pending; retry other titles','metadata_pending')
    raise diagnostic_tag(SourceFailure('No working compatible torrent source available'),'source_candidates_exhausted')


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
    if getattr(BUDGET,'remote_verification',False) and not selected.get('local'):
        from verification_remote import execute as remote_execute
        with REL.lock('torrent:'+selected['hash']):
            return remote_execute(globals(),key,selected,'probe',{},deadline or time.monotonic()+WORK_SECONDS)
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
    start_foreground_cleanup(key)
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


def prepared(key,background=False,audio_index=None,cancel=None,source_id=None):
    if source_id:
        option=RUSSIAN.prepare_option(key,source_id)
        if option:return option
        raise Checkpoint('Verified Russian-audio option is no longer current')
    if background and F.verification_held(key):raise Checkpoint('Title temporarily protected from background verification')
    speculative=getattr(BUDGET,'speculative',False)
    deadline=time.monotonic()+(DETAIL_SECONDS if speculative else WORK_SECONDS if background else PREPARATION_SECONDS)
    if background:deadline=min(deadline,getattr(BUDGET,'worker_deadline',deadline))
    cleanup=None if background else start_foreground_cleanup(key)
    F.consume_rechecks()
    with failure_scope(BUDGET),validation_attempt(background),request_budget(deadline,cancel),playback_phase(key,'preparation'):
        # Give idle background transport a short head start on releasing memory.
        # Cleanup remains asynchronous if TorrServer is slow or unavailable.
        if cleanup is not None and not cleanup.is_set():cleanup.wait(min(1.5,remaining(1.5)))
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
        except (Checkpoint,TimeoutError,subprocess.TimeoutExpired,ClientCancelled,sqlite3.OperationalError) as error:
            if isinstance(error,sqlite3.OperationalError):
                error=Checkpoint('Catalogue database is temporarily busy','coordinator_database');error.failure_category='coordinator';raise error from None
            if not speculative:
                F.enqueue(key,200)
                REL.checkpoint(key,state='checkpoint',message='Preparation continues in background; retry Play later',audio_index=audio_index)
            if isinstance(error,ClientCancelled) or (cancel and cancel()):raise ClientCancelled('Client disconnected') from None
            if isinstance(error,Checkpoint):raise
            code='subprocess_timeout' if isinstance(error,subprocess.TimeoutExpired) else 'preparation_budget'
            raise Checkpoint('Preparation continues in background',code) from None
        finally:lock.release()


def durable_playback_evidence(key,audio_index=None):
    """Reuse accepted exact-source evidence; audits add fresh speed evidence.

    Playback rows survive the five-minute response cache and service reboots.
    They are metadata, not a promise that a torrent is currently reachable.
    Resolution and the stream proxy still check the live transport.
    """
    if F.source_state(key) not in ('verified','downloaded'):return None
    with F.db() as conn:
        row=conn.execute('SELECT p.value,v.proof,v.state FROM playback p JOIN validation v ON v.key=p.key WHERE p.key=?',(key,)).fetchone()
    if not row or row[2]!='verified':return None
    try:
        saved=json.loads(row[0]);proof=json.loads(row[1] or '{}')
        selected=saved['selected'];sid=REL.sid(selected)
        audio=saved.get('DefaultAudioStreamIndex')
        if (saved.get('verification_version') not in (5,VERSION) or saved.get('audio_policy')!=2
            or proof.get('version') not in (5,VERSION) or proof.get('audio_policy')!=2
            or (proof.get('version')==6 and not REL.speed_proof_valid(proof,bool(selected.get('local'))))
            or proof.get('source_id')!=sid or proof.get('source_hash')!=selected.get('hash')
            or proof.get('file_index')!=selected.get('index') or proof.get('filename')!=selected.get('filename')
            or proof.get('length')!=selected.get('length')
            or proof.get('selected_audio_index')!=audio
            or (audio_index is not None and audio_index!=audio)
            or proof.get('audio_conversion')!=saved.get('AudioConversionRequired')
            or proof.get('checked_at')!=saved.get('checked_at')
            or not proof.get('checked_at') or REL.excluded(key,selected)
            or bool(F.local_download(key))!=bool(selected.get('local'))):return None
        samples=proof.get('samples',[])
        if (len(samples)!=2 or samples[0].get('position')!=0 or samples[1].get('position',0)<=0
            or any(s.get('video_frames',0)<=0 or (audio is not None and s.get('audio_frames',0)<=0) for s in samples)):return None
        # Older durable rows predate verification_info. Their native stream
        # metadata contains the codec/track/duration fields needed here.
        info=saved.get('verification_info')
        if not info:
            streams=[]
            fields={'Index':'index','Codec':'codec_name','Profile':'profile','Channels':'channels',
                    'PixelFormat':'pix_fmt','Level':'level','Width':'width','Height':'height',
                    'BitRate':'bit_rate','SampleRate':'sample_rate','AverageFrameRate':'avg_frame_rate'}
            for track in saved['MediaStreams']:
                if track['Type'] not in ('Video','Audio') and (track['Type']!='Subtitle' or track.get('IsExternal')):continue
                stream={target:track[source] for source,target in fields.items() if track.get(source) is not None}
                stream.update(codec_type=track['Type'].lower(),tags={'language':track.get('Language','und')},
                    disposition={'default':int(bool(track.get('IsDefault'))),'forced':int(bool(track.get('IsForced')))})
                streams.append(stream)
            info={'streams':streams,'format':{'duration':saved['RunTimeTicks']/10000000,'bit_rate':saved.get('Bitrate',5000000)}}
        if not any(s.get('codec_type')=='video' for s in info['streams']):return None
        return {'selected':selected,'info':info,'proof':proof,'transport_check':True}
    except (ValueError,KeyError,TypeError,AttributeError,IndexError):return None


def playback_proof_anchor(proof):
    return hashlib.sha256(json.dumps(proof,sort_keys=True,separators=(',',':')).encode()).hexdigest()


def cached_audio_anchor_current(key,prepared_key,output):
    accepted=durable_playback_evidence(key)
    return bool(accepted and REL.sid(accepted['selected'])==REL.sid(output['selected'])
                and cached('audio-anchor:'+prepared_key)==playback_proof_anchor(accepted['proof']))


def playback_audio_proof(key,verified,audio_index,conversion,deadline):
    """Check a new track without re-auditing an already accepted source.

    The caller supplies current durable exact-source evidence. This track-only
    evidence is short-lived and bound to the entire accepted proof; it never
    replaces the catalogue audit or its canonical playback row.
    """
    selected,info,accepted=verified['selected'],verified['info'],verified['proof']
    anchor=playback_proof_anchor(accepted)
    cachekey='playback-audio:'+key+':'+REL.sid(selected)+':'+str(audio_index)+':'+str(conversion)
    saved=cached(cachekey)
    if saved and saved.get('anchor')==anchor:return saved['proof']
    track=next((s for s in info['streams'] if s.get('codec_type')=='audio' and s.get('index')==audio_index),None)
    if track is None:raise ValueError('Audio track not found')
    if conversion and track.get('codec_name') not in AUDIO_CODECS:
        raise Checkpoint('Selected audio conversion is unsupported','playback_audio_unsupported')
    samples=[]
    for old,seconds in zip(accepted['samples'],(6,2)):
        samples.append(REL.sample(key,selected,info,audio_index,conversion,old['position'],seconds,deadline))
    proof={**accepted,'selected_audio_index':audio_index,'audio_conversion':conversion,
           'samples':samples,'playback_audio_checked_at':time.time()}
    remember(cachekey,{'anchor':anchor,'proof':proof},300)
    return proof


def prepare_locked(key,background,audio_index,deadline):
    if not background and F.manual_recheck_state(key) in ('pending','failed'):
        raise Checkpoint('Waiting for requested fresh source verification')
    prepared_key='prepared:'+key+(':'+str(audio_index) if audio_index is not None else '')
    with LIVE_LOCK:
        if LIVE['key'] not in (None,key) and LIVE['active']>0:
            raise PlaybackBusy('Another title is currently streaming; this server supports one stream')
    saved=cached(prepared_key)
    if not background and saved and saved.get('verification_version') in (5,VERSION) and saved.get('audio_policy')==2 and saved.get('subtitle_policy')==5 and saved.get('default_audio_policy')==1 and (not saved.get('playback_audio_variant') or cached_audio_anchor_current(key,prepared_key,saved)) and F.source_state(key) in ('verified','downloaded'):
        playback_event(key,'prepared_cache',time.monotonic(),'hit')
        return saved
    verified=cached('verified-'+prepared_key) if not background else None
    if verified and (verified.get('proof',{}).get('version') not in (5,VERSION) or F.source_state(key) not in ('verified','downloaded')
                     or REL.excluded(key,verified['selected']) or bool(F.local_download(key))!=bool(verified['selected'].get('local'))):verified=None
    if not verified:
        verified=durable_playback_evidence(key,None if not background else audio_index)
        if verified and background and verified['selected'].get('local'):verified=None
        if verified:playback_event(key,'durable_evidence',time.monotonic(),'hit')
    for attempt in range(3):
        try:
            with playback_phase(key,'torrent_resolution'):
                selected=verified['selected'] if verified and not verified.get('transport_check') else resolve(key,deadline=deadline)
                if verified and REL.sid(selected)!=REL.sid(verified['selected']):verified=None
        except (ClientCancelled,PlaybackBusy,Checkpoint,TimeoutError,subprocess.TimeoutExpired,sqlite3.OperationalError):raise
        except Exception as error:
            cancel=getattr(BUDGET,'cancel',None)
            if cancel and cancel():raise ClientCancelled('Client disconnected') from None
            if getattr(BUDGET,'speculative',False):raise
            remaining(1)
            if background and isinstance(error,SourceFailure):
                remember('source-status:'+key,{'state':'unavailable','checked_at':time.time()},900)
                F.validation_result(key,False,reason='Source preparation failed ('+type(error).__name__+')')
            elif not background:playback_failure(key,error)
            raise
        try:
            BUDGET.diagnostic_source_hash=selected.get('hash')
            with playback_phase(key,'video_probe'):info=verified['info'] if verified else probe(selected,key,deadline=deadline)
            video=next((s for s in info.get('streams',[]) if s.get('codec_type')=='video' and not s.get('disposition',{}).get('attached_pic')),{})
            if video.get('codec_name')=='h264' and video.get('pix_fmt') not in ('yuv420p','yuvj420p'):
                raise diagnostic_tag(DecodeFailure('Unsupported H264 pixel format'),'source_video_format')
            try:validate_video(video)
            except ValueError:raise diagnostic_tag(DecodeFailure('Source requires unsupported video decoding'),'source_video_format') from None
            audio=[t for t in info.get('streams',[]) if t.get('codec_type')=='audio']
            ordered=sorted(audio,key=lambda t:0 if t.get('codec_name')=='aac' and t.get('profile')=='LC' else 1)
            selected_audio=next((t for t in ordered if t['index']==audio_index),None) if audio_index is not None else next((t for lang in ('eng','en','heb','he') for t in ordered if t.get('tags',{}).get('language')==lang),ordered[0] if ordered else None)
            # Prefer English when available; otherwise retain the available
            # accepted track instead of changing languages unnecessarily.
            if verified and not background and audio_index is None and not any(t.get('tags',{}).get('language') in ('eng','en') for t in audio):
                accepted_audio=verified['proof'].get('selected_audio_index')
                selected_audio=next((t for t in audio if t['index']==accepted_audio),selected_audio)
            if audio_index is not None and selected_audio is None:raise ValueError('Audio track not found')
            needs_conversion=selected_audio is not None and (selected_audio.get('codec_name')!='aac' or selected_audio.get('profile')!='LC')
            if needs_conversion and float(info.get('format',{}).get('duration') or 0)<=0:raise diagnostic_tag(SourceFailure('Converted VOD requires known duration'),'source_duration_missing')
            track_index=selected_audio['index'] if selected_audio else None
            changed_audio=bool(verified and not background and (verified['proof'].get('selected_audio_index')!=track_index or verified['proof'].get('audio_conversion')!=needs_conversion))
            if changed_audio:
                # Revalidate the durable anchor even when a response-cache
                # entry supplied the source. A stale/manual-rechecked/excluded
                # source cannot gain track evidence through a cached response.
                anchor=durable_playback_evidence(key)
                if not anchor or REL.sid(anchor['selected'])!=REL.sid(selected):
                    raise Checkpoint('Accepted source changed during audio selection','playback_audio_source_changed')
                try:proof=playback_audio_proof(key,anchor,track_index,needs_conversion,deadline)
                except SourceFailure:
                    raise Checkpoint('Selected audio could not be checked; accepted source retained','playback_audio_check') from None
                verified={**anchor,'audio_variant':True}
            else:
                proof=verified['proof'] if verified and not background else REL.verify(key,selected,info,track_index,needs_conversion,deadline)
            break
        except (ClientCancelled,PlaybackBusy,Checkpoint,TimeoutError,subprocess.TimeoutExpired,sqlite3.OperationalError):raise
        except Exception as error:
            cancel=getattr(BUDGET,'cancel',None)
            if cancel and cancel():raise ClientCancelled('Client disconnected') from None
            if getattr(BUDGET,'speculative',False):raise
            remaining(1)
            record_source_failure(BUDGET,error,'title validation',selected.get('hash'))
            playback_event(key,'source_rejected',time.monotonic(),'failed',error)
            if not isinstance(error,SourceFailure):raise
            request_mediafusion_after_slow_primary(
                key,selected,'speed_rejected' if isinstance(error,SourceTooSlow) else None,background)
            verified=None
            REL.exclude(key,selected,21600 if isinstance(error,SourceTooSlow) else 900)
            remember('source-excluded:'+key+':'+selected['hash'],True,21600 if isinstance(error,SourceTooSlow) else 900)
            with db() as conn:conn.execute('DELETE FROM cache WHERE key=?',('resolved:'+key,))
            # Retain torrents/readers: another request may still be playing an
            # older bound source. Only this preparation advances to a fallback.
            if attempt==2:
                if background and isinstance(error,SourceFailure):
                    remember('source-status:'+key,{'state':'unavailable','checked_at':time.time()},900)
                    F.validation_result(key,False,reason='Video preflight failed ('+type(error).__name__+')')
                elif not background:playback_failure(key,error)
                raise
    return finish_preparation(key,background,deadline,selected,info,proof,selected_audio,needs_conversion,verified,prepared_key)


def finish_preparation(key,background,deadline,selected,info,proof,selected_audio,needs_conversion,verified=None,prepared_key=None):
    prepared_key=prepared_key or 'prepared:'+key
    tracks=[]; languages=set()
    for stream in info.get('streams',[]):
        typ=stream.get('codec_type')
        if typ not in ('video','audio','subtitle'): continue
        if typ=='video' and stream.get('disposition',{}).get('attached_pic'): continue
        # Jellyfin extracts embedded tracks from the indexed .strm path, which
        # is dynamiclibrary:// for online titles. That path cannot supply the
        # source bytes, so advertise only delivered external subtitles there.
        if typ=='subtitle' and not selected.get('local'): continue
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
    # Once unsupported online embedded tracks are omitted, external subtitle
    # indexes must follow the visible stream list. Android TV uses the index
    # to access that list and crashes if a removed track leaves a large gap.
    next_index=max(t['Index'] for t in tracks)+1
    tracks=[t for t in tracks if t['Type']!='Subtitle' or t['Language']!='heb' or t['Index'] in keep]
    if not background and len(hebrew)<2:
        pool=ThreadPoolExecutor(max_workers=1)
        cancel=getattr(BUDGET,'cancel',None)
        def subtitles():
            with request_budget(deadline,cancel):
                try:extracted=EMBED.ready(key,selected)
                except (OSError,ValueError,sqlite3.Error):extracted=[]
                try:return extracted+external_subtitles(key,selected['filename'],'eng' in languages,max_hebrew=2-len(hebrew))
                except Exception:
                    if extracted:return extracted
                    raise
        try:
            with playback_phase(key,'subtitle_lookup'):
                external=pool.submit(subtitles).result(timeout=max(.001,min(15,deadline-time.monotonic())))
            he_count=len(hebrew);variants=set()
            for sub in external:
                if sub.get('variant') in variants:continue
                if sub.get('variant'):variants.add(sub['variant'])
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
        'languages':sorted(languages),'checked_at':proof.get('checked_at',time.time()),'audio_policy':2,'subtitle_policy':5,
        'default_audio_policy':1,'playback_audio_variant':bool(verified and verified.get('audio_variant')),
        'verification_version':proof.get('version',VERSION),'PlaybackUrl':REL.source_url('play',key,selected),
        'DefaultAudioStreamIndex':selected_audio['index'] if selected_audio else None,'AudioConversionRequired':needs_conversion}
    if not background:
        try:output['RussianAlternatives']=RUSSIAN.options(key,proof,selected)
        except (OSError,ValueError,TypeError,sqlite3.Error):output['RussianAlternatives']=[]
    if getattr(BUDGET,'speculative',False):remaining(1)
    audio_variant=bool(verified and verified.get('audio_variant'))
    if not audio_variant:
        with db() as conn:
            conn.execute('INSERT OR REPLACE INTO playback VALUES (?,?,?)',(key,json.dumps({**output,'verification_info':info}),time.time()))
    accepted=True if verified and not background else F.validation_result(key,True,proof=proof)
    if accepted is not False:
        REL.success(key,selected)
        # Windows-local results enqueue naturally through the verified playback
        # row; only persist immediately when this server already has metadata.
        retain_torrent_metadata(selected)
        if not audio_variant:remember('verified-'+prepared_key,{'selected':selected,'info':info,'proof':proof},max(1,300-(time.time()-proof.get('checked_at',time.time()))))
        if not background:
            try:EMBED.enqueue(key,selected,output['RunTimeTicks']/10000000)
            except (OSError,ValueError,sqlite3.Error):log('Embedded subtitle scheduling deferred')
    if audio_variant:remember('audio-anchor:'+prepared_key,playback_proof_anchor(verified['proof']),300)
    return output if background else remember(prepared_key,output,300 if audio_variant else max(1,300-(time.time()-output['checked_at'])))


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
            BUDGET.diagnostic_internal=bool(parts and parts[0]=='raw')
            if parsed.path=='/ready':
                ready=F.ready_keys()
                ready.update(":".join(key.split(":")[:2]) for key in list(ready))
                wanted=U.parse_qs(parsed.query).get('keys',[''])[0]
                if wanted:ready.intersection_update(wanted.split(','))
                return self.reply({'titles':sorted(ready)})
            if parsed.path=='/series-counts':
                wanted=U.parse_qs(parsed.query).get('ids',[''])[0].split(',')[:1000]
                return self.reply(F.series_episode_counts(wanted))
            if parsed.path=='/title-labels':
                keys=U.parse_qs(parsed.query).get('keys',[''])[0].split(',')[:100]
                labels={}
                with F.db() as conn:
                    for key in keys:
                        if not re.fullmatch(r'(movie|series):tt\d{5,12}',key):continue
                        kind,imdb=key.split(':',1)
                        row=conn.execute('SELECT COALESCE(l.title_he,a.hebrew),l.description_he,a.english FROM aliases a LEFT JOIN localized_metadata l ON l.imdb=a.imdb AND l.kind=? WHERE a.imdb=?',(kind,imdb)).fetchone()
                        if row:labels[key]={'hebrew':row[0],'description':row[1],'english':row[2]}
                return self.reply(labels)
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
                selected_source=U.parse_qs(parsed.query).get('source',[''])[0]
                info=prepared(key,audio_index=int(audio) if audio is not None else None,cancel=self.disconnected,
                    source_id=selected_source or None)
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
                upstream_response=REL.open_sample(self,upstream_request,max(.001,data_deadline-time.monotonic()))
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
        except Checkpoint as error:
            if key:playback_event(key,'request',stream_started,'checkpoint',error)
            if not response_started:self.reply({'error':'Preparing source in background; retry Play shortly.'},503)
        except PlaybackBusy as error:
            if key:playback_event(key,'request',stream_started,'checkpoint',error)
            if not response_started:self.reply({'error':'Another playback preparation is in progress; retry shortly.'},409)
        except urllib.error.HTTPError as error:
            if key:playback_event(key,'request',stream_started,'failed',error)
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
            if key and parts and parts[0]=='play':playback_failure(key,error)
            if key:playback_event(key,'request',stream_started,'failed',error)
            else:log('Request failed: '+type(error).__name__)
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
    parser.add_argument('command',choices=('serve','sync','collections','badges','trailers','discovery-sync','availability','downloads','imdb-sync','metadata-import','metadata-prepare','metadata-prepare-stop','remote-availability','embedded-subtitles','hebrew-metadata','torrent-metadata','russian-audio','cache-prune'),default='serve',nargs='?')
    parser.add_argument('--movies',type=int,default=1000)
    parser.add_argument('--shows',type=int,default=200)
    parser.add_argument('--slot',type=int,choices=range(1,33),default=1)
    parser.add_argument('--limit',type=int,default=8)
    parser.add_argument('--batch-size',type=int,default=500)
    args=parser.parse_args()
    BUDGET.metadata_background=args.command!='serve'
    if args.command=='metadata-prepare-stop':
        return metadata_prepare_stop(args.slot)
    if args.command=='metadata-prepare':return metadata_prepare(args.slot)
    if args.command=='metadata-import':
        log('LAN metadata imports applied: '+str(metadata_import(seed=True)))
        return
    if args.command=='sync': return sync(args.movies,args.shows)
    if args.command=='collections': return update_collections()
    if args.command=='badges': return refresh_subtitle_badges()
    if args.command=='trailers': return refresh_trailers()
    if args.command=='discovery-sync': return F.sync_discovery()
    if args.command=='availability': return F.warm_availability()
    if args.command=='embedded-subtitles': return log('Embedded subtitle work steps: '+str(EMBED.work()))
    if args.command=='torrent-metadata':return log('Torrent metadata retained: '+str(backfill_torrent_metadata()))
    if args.command=='russian-audio':return log('Russian-audio worker checked one title: '+str(RUSSIAN.work()))
    if args.command=='cache-prune':return log('Expired temporary cache rows pruned: '+str(prune_expired_cache(limit=args.limit,batch_size=args.batch_size)))
    if args.command=='hebrew-metadata': return F.refresh_hebrew_metadata(limit=args.limit)
    if args.command=='remote-availability': return F.warm_availability(remote=True,slot=args.slot)
    if args.command=='downloads': return F.manage_downloads()
    if args.command=='imdb-sync': return F.update_index()
    db().close()
    initialize_provider_generation()
    server=ThreadingHTTPServer(('0.0.0.0',8092),Handler)
    server.daemon_threads=True
    log('Catalogue bridge ready')
    server.serve_forever()


F=Features(globals())
REL=Reliability(globals())
EMBED=EmbeddedSubtitles(globals())
RUSSIAN=RussianAudio(globals())
DIAG=PlaybackDiagnostics(globals())
if __name__=='__main__': main()
