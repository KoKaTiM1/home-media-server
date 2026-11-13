"""Request-scoped progress for local probes; never use torrent-wide counters."""
import http.client
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import re
import select
import socket
import threading
import time
import urllib.parse
import uuid
from pathlib import Path

LIFECYCLE_LIMIT=1024*1024
LIFECYCLE_TTL=24*60*60
OBSERVATION_MAX_SECONDS=60*60

def worker_lifecycle_paths(state_dir):
    """Locate worker-wide lifecycle artifacts from its per-job state folder."""
    data_root=Path(state_dir).parents[2]
    return (data_root/'proxy-lifecycle.jsonl',
            data_root/'proxy-lifecycle-observe-until')

class LifecycleRecorder:
    """Write a short-lived, allowlisted request lifecycle trace when opted in."""
    def __init__(self,path,observe_until_path,trace_id=None):
        self.path=Path(path);self.observe_until_path=Path(observe_until_path)
        self.trace_id=trace_id or uuid.uuid4().hex[:16];self.lock=threading.Lock()
        self.expires_at=self._read_expiry()
    def _read_expiry(self):
        try:
            value=float(self.observe_until_path.read_text().strip())
            now=time.time()
            if now < value <= now+OBSERVATION_MAX_SECONDS:return value
        except (OSError,ValueError,TypeError):pass
        return 0
    def emit(self,event,**fields):
        if not self.expires_at or time.time()>=self.expires_at:return
        allowed={'request_id','route_id','method','reason','elapsed_ms','bytes_sent','active_handlers'}
        row={'time':round(time.time(),3),'trace':self.trace_id,'event':event}
        row.update({k:v for k,v in fields.items() if k in allowed and isinstance(v,(str,int,float))})
        encoded=(json.dumps(row,separators=(',',':'),allow_nan=False)+'\n').encode()
        if len(encoded)>4096:return
        with self.lock:
            try:
                self.path.parent.mkdir(parents=True,exist_ok=True)
                now=time.time();kept=[]
                if self.path.exists():
                    for line in self.path.read_bytes().splitlines(keepends=True):
                        try:stamp=float(json.loads(line).get('time',0))
                        except (ValueError,TypeError,AttributeError):continue
                        if now-stamp<=LIFECYCLE_TTL:kept.append(line if line.endswith(b'\n') else line+b'\n')
                kept.append(encoded);total=sum(map(len,kept))
                while total>LIFECYCLE_LIMIT and kept:
                    total-=len(kept.pop(0))
                temporary=self.path.with_name(self.path.name+'.tmp')
                temporary.write_bytes(b''.join(kept));temporary.replace(self.path)
                try:self.path.chmod(0o600)
                except OSError:pass
            except OSError:pass

def prune_lifecycle_artifacts(data_folder,now=None):
    """Expire the opt-in window and its bounded trace without touching task data."""
    now=time.time() if now is None else now;folder=Path(data_folder)
    marker=folder/'proxy-lifecycle-observe-until';trace=folder/'proxy-lifecycle.jsonl'
    try:
        expires=float(marker.read_text().strip())
        if expires<=now or expires>now+OBSERVATION_MAX_SECONDS:marker.unlink(missing_ok=True)
    except (OSError,ValueError,TypeError):
        try:marker.unlink(missing_ok=True)
        except OSError:pass
    try:
        if now-trace.stat().st_mtime>=LIFECYCLE_TTL:trace.unlink(missing_ok=True)
    except OSError:pass

class Progress:
    def __init__(self,url,length,lifecycle=None):
        self.url=url;self.length=length;self.ranges=[];self.last=time.monotonic()
        self.bytes=0;self.error=None;self.stop=threading.Event();self.lock=threading.RLock()
        self.lifecycle=lifecycle or _NullRecorder();self.route_id=uuid.uuid4().hex[:12];self.stop_reason=None
    def request_stop(self,reason):
        with self.lock:
            if self.stop.is_set():return
            self.stop_reason=reason;self.stop.set()
        self.lifecycle.emit('request_stop_signal',route_id=self.route_id,reason=reason)
    def delivered(self,start,end):
        with self.lock:
            merged=[]
            for lo,hi in sorted(self.ranges+[(start,end)]):
                if merged and lo<=merged[-1][1]:merged[-1]=(merged[-1][0],max(hi,merged[-1][1]))
                else:merged.append((lo,hi))
            if len(merged)>128:self.request_stop('range_limit');return
            size=sum(hi-lo for lo,hi in merged)
            if size>self.bytes:self.last=time.monotonic()
            self.bytes=size;self.ranges=merged
    def stalled(self,seconds=45):
        with self.lock:return time.monotonic()-self.last>=seconds

def response_offset(status,content_range,request_range,length):
    requested=re.fullmatch(r'bytes=(\d+)-(\d*)',request_range or '')
    if request_range and not requested:raise ValueError('range_request')
    start=int(requested[1]) if requested else 0
    if status==206:
        m=re.fullmatch(r'bytes (\d+)-(\d+)/(\d+)',content_range or '')
        if not m:raise ValueError('range_response')
        lo,hi,total=map(int,m.groups())
        if total!=length or lo!=start or not lo<=hi<length or (requested and requested[2] and hi>int(requested[2])):raise ValueError('range_response')
        return lo,hi+1
    if status==200 and start==0 and (not requested or not requested[2] or int(requested[2])>=length-1):return 0,length
    raise ValueError('range_response')

class Handler(BaseHTTPRequestHandler):
    def log_message(self,*args):pass
    def do_HEAD(self):self.copy()
    def do_GET(self):self.copy()
    def copy(self):
        progress=self.server.routes.get(self.path)
        if not progress or progress.stop.is_set():self.send_error(404);return
        request_id=uuid.uuid4().hex[:12];started=time.monotonic();sent=0;reason=None
        self.server.lifecycle.emit('handler_start',request_id=request_id,route_id=progress.route_id,method=self.command)
        with self.server.active_lock:self.server.active_handlers+=1
        origin=urllib.parse.urlsplit(progress.url);connection=http.client.HTTPConnection(origin.hostname,origin.port,timeout=60)
        done=threading.Event()
        reason_lock=threading.Lock()
        def set_reason(value):
            nonlocal reason
            with reason_lock:
                if reason is None:
                    reason=value
                    if value in ('verifier_stop','proxy_shutdown','client_disconnect'):
                        self.server.lifecycle.emit('handler_stop_observed',request_id=request_id,route_id=progress.route_id,reason=value)
        def watch():
            while not done.wait(.2):
                if progress.stop.is_set():set_reason('verifier_stop')
                elif self.server.stopping.is_set():set_reason('proxy_shutdown')
                elif select.select([self.connection],[],[],0)[0]:set_reason('client_disconnect')
                if reason is not None:
                    try:
                        if connection.sock:connection.sock.shutdown(socket.SHUT_RDWR)
                    except OSError:pass
                    connection.close();return
        watcher=threading.Thread(target=watch,daemon=True);watcher.start()
        try:
            headers={}
            if self.headers.get('Range'):headers['Range']=self.headers['Range']
            connection.request(self.command,urllib.parse.urlunsplit(('', '',origin.path,origin.query,'')),headers=headers)
            response=connection.getresponse()
            lo,limit=response_offset(response.status,response.getheader('Content-Range'),headers.get('Range'),progress.length)
            self.send_response(response.status)
            for name in ('Content-Type','Content-Length','Content-Range','Accept-Ranges'):
                if response.getheader(name):self.send_header(name,response.getheader(name))
            self.end_headers()
            if self.command!='HEAD':
                while not progress.stop.is_set() and not self.server.stopping.is_set():
                    part=response.read1(min(65536,limit-lo)) if lo<limit else b''
                    if not part:
                        set_reason('response_complete' if lo>=limit else 'upstream_eof');break
                    self.wfile.write(part);sent+=len(part);progress.delivered(lo,lo+len(part));lo+=len(part)
            if reason is None:
                if progress.stop.is_set():set_reason('verifier_stop')
                elif self.server.stopping.is_set():set_reason('proxy_shutdown')
                else:set_reason('response_complete' if self.command=='HEAD' or lo>=limit else 'handler_exit')
        except ValueError:
            progress.error='range_mismatch';self.close_connection=True;set_reason('range_mismatch')
        except (OSError,http.client.HTTPException):
            progress.error='local_transport';self.close_connection=True;set_reason('request_transport')
        finally:
            done.set();connection.close();watcher.join(timeout=1)
            self.server.lifecycle.emit('handler_close',request_id=request_id,route_id=progress.route_id,reason=reason or 'handler_exit',elapsed_ms=round((time.monotonic()-started)*1000),bytes_sent=sent)
            with self.server.active_lock:self.server.active_handlers=max(0,self.server.active_handlers-1)

class ProgressProxy:
    def __init__(self,lifecycle_path=None,observe_until_path=None):
        self.lifecycle=(LifecycleRecorder(lifecycle_path,observe_until_path) if lifecycle_path and observe_until_path else _NullRecorder())
        self.server=ThreadingHTTPServer(('127.0.0.1',0),Handler);self.server.daemon_threads=True
        self.server.routes={};self.server.stopping=threading.Event();self.server.lifecycle=self.lifecycle
        self.server.active_handlers=0;self.server.active_lock=threading.Lock()
        self.prefix='http://127.0.0.1:'+str(self.server.server_port)
        self.thread=threading.Thread(target=self.server.serve_forever,kwargs={'poll_interval':.1},daemon=True);self.thread.start()
        self.lifecycle.emit('proxy_start')
    def source(self,url,length):
        parsed=urllib.parse.urlsplit(url)
        if parsed.scheme!='http' or not parsed.hostname or parsed.username or parsed.password or type(length) is not int or length<=0:raise ValueError('Invalid progress source')
        path='/'+uuid.uuid4().hex;self.server.routes[path]=Progress(url,length,self.lifecycle);return self.prefix+path
    def operation(self,command):
        return next((self.server.routes.get(x[len(self.prefix):]) for x in command if isinstance(x,str) and x.startswith(self.prefix+'/')),None)
    def close(self):
        started=time.monotonic()
        self.server.stopping.set()
        self.lifecycle.emit('proxy_stop_signal',reason='verifier_exit',active_handlers=self.server.active_handlers)
        for p in self.server.routes.values():p.request_stop('verifier_exit')
        self.server.shutdown();self.server.server_close();self.thread.join(timeout=2)
        self.lifecycle.emit('proxy_close',elapsed_ms=round((time.monotonic()-started)*1000),active_handlers=self.server.active_handlers)

class _NullRecorder:
    def emit(self,*args,**kwargs):pass
