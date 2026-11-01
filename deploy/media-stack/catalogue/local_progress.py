"""Request-scoped progress for local probes; never use torrent-wide counters."""
import http.client
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import re
import select
import socket
import threading
import time
import urllib.parse
import uuid

class Progress:
    def __init__(self,url,length):
        self.url=url;self.length=length;self.ranges=[];self.last=time.monotonic()
        self.bytes=0;self.error=None;self.stop=threading.Event();self.lock=threading.Lock()
    def delivered(self,start,end):
        with self.lock:
            merged=[]
            for lo,hi in sorted(self.ranges+[(start,end)]):
                if merged and lo<=merged[-1][1]:merged[-1]=(merged[-1][0],max(hi,merged[-1][1]))
                else:merged.append((lo,hi))
            if len(merged)>128:self.stop.set();return
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
        origin=urllib.parse.urlsplit(progress.url);connection=http.client.HTTPConnection(origin.hostname,origin.port,timeout=60)
        done=threading.Event()
        def watch():
            while not done.wait(.2):
                if progress.stop.is_set() or self.server.stopping.is_set() or select.select([self.connection],[],[],0)[0]:
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
                    if not part:break
                    self.wfile.write(part);progress.delivered(lo,lo+len(part));lo+=len(part)
        except ValueError:
            progress.error='range_mismatch';self.close_connection=True
        except (OSError,http.client.HTTPException):
            progress.error='local_transport';self.close_connection=True
        finally:
            done.set();connection.close();watcher.join(timeout=1)

class ProgressProxy:
    def __init__(self):
        self.server=ThreadingHTTPServer(('127.0.0.1',0),Handler);self.server.daemon_threads=True
        self.server.routes={};self.server.stopping=threading.Event()
        self.prefix='http://127.0.0.1:'+str(self.server.server_port)
        self.thread=threading.Thread(target=self.server.serve_forever,kwargs={'poll_interval':.1},daemon=True);self.thread.start()
    def source(self,url,length):
        parsed=urllib.parse.urlsplit(url)
        if parsed.scheme!='http' or not parsed.hostname or parsed.username or parsed.password or type(length) is not int or length<=0:raise ValueError('Invalid progress source')
        path='/'+uuid.uuid4().hex;self.server.routes[path]=Progress(url,length);return self.prefix+path
    def operation(self,command):
        return next((self.server.routes.get(x[len(self.prefix):]) for x in command if isinstance(x,str) and x.startswith(self.prefix+'/')),None)
    def close(self):
        self.server.stopping.set()
        for p in self.server.routes.values():p.stop.set()
        self.server.shutdown();self.server.server_close();self.thread.join(timeout=2)
