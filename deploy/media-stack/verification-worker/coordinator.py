"""Scoped HTTPS decoder gateway; no catalogue DB or Jellyfin credentials."""
import argparse
import hashlib
import hmac
import http.client
from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
import json
import os
from pathlib import Path
import re
import select
import socket
import ssl
import threading
from urllib.parse import urlsplit
from verification_jobs import DecodeJobs,InvalidJob,PROTOCOL,PREFIX

class Server(ThreadingHTTPServer):
    daemon_threads=True
    def verify_request(self,request,address):return address[0] in self.allowed and self.slots.acquire(blocking=False)
    def process_request_thread(self,request,address):
        try:
            request.settimeout(60)
            with self.context.wrap_socket(request,server_side=True) as secured:
                super().process_request_thread(secured,address)
        except (OSError,ssl.SSLError):request.close()
        finally:self.slots.release()
    def handle_error(self,*args):pass

class Handler(BaseHTTPRequestHandler):
    protocol_version='HTTP/1.0'
    def log_message(self,*args):pass
    def reply(self,status,value):
        if self.path==PREFIX+'/result' or status>=400:
            print('Verification request '+self.path.rsplit('/',1)[-1]+' status='+str(status)+' ack='+str(value.get('ack','')),flush=True)
        raw=json.dumps({'protocol':PROTOCOL,**value},allow_nan=False).encode()
        self.send_response(status);self.send_header('Content-Type','application/json');self.send_header('Content-Length',str(len(raw)));self.send_header('Cache-Control','no-store');self.end_headers();self.wfile.write(raw)
    def authorized(self):
        token=self.headers.get('Authorization','')
        return hmac.compare_digest(hashlib.sha256(token.encode()).digest(),self.server.auth)
    def do_HEAD(self):self.handle_api()
    def do_GET(self):self.handle_api()
    def do_POST(self):self.handle_api()
    def handle_api(self):
        self.connection.settimeout(60)
        try:
            if not self.authorized():return self.reply(401,{'error':'Verification worker authorization required'})
            if self.command in ('GET','HEAD') and self.path.startswith(PREFIX+'/stream/'):
                return self.stream()
            if self.command=='GET' and self.path==PREFIX+'/status':return self.reply(200,self.server.jobs.status())
            if self.command!='POST' or self.headers.get('Transfer-Encoding'):return self.reply(404,{'error':'Unknown endpoint'})
            size=self.headers.get('Content-Length','')
            if not size.isdigit() or not 0<int(size)<=2*1024*1024:raise InvalidJob('Invalid request size')
            p=json.loads(self.rfile.read(int(size)))
            if not isinstance(p,dict) or p.get('protocol')!=PROTOCOL:raise InvalidJob('Invalid protocol')
            worker=p.get('worker_id');q=self.server.jobs;q.seen(worker)
            if self.path==PREFIX+'/claim':
                task=q.claim(worker);return self.reply(200,{'task':task,'status':q.status()})
            if self.path==PREFIX+'/renew':return self.reply(200,{'active':q.renew(worker,p.get('task',{}),p.get('phase'))})
            if self.path==PREFIX+'/result':
                result=p.get('result',{});ack=q.receive(worker,p.get('task',{}),result)
                return self.reply(200,{'ack':ack,'result_id':result['result_id']})
            return self.reply(404,{'error':'Unknown endpoint'})
        except (ValueError,KeyError,TypeError):self.reply(400,{'error':'Invalid verification request'})
        except (OSError,TimeoutError):pass
        except Exception as error:
            print('Verification request error '+type(error).__name__,flush=True)
            self.reply(503,{'error':'Verification service temporarily unavailable'})
    def stream(self):
        fields=self.path[len(PREFIX+'/stream/'):].split('/')
        if len(fields)!=2 or not all(re.fullmatch('[a-f0-9]{32}',s) for s in fields):raise InvalidJob('Invalid stream')
        job,lease=fields;q=self.server.jobs
        url=q.source(job,lease)
        if not url:return self.reply(410,{'error':'Stream lease ended'})
        target=urlsplit(url);origin=urlsplit(self.server.bridge)
        if (target.scheme,target.hostname,target.port)!=(origin.scheme,origin.hostname,origin.port) or not target.path.startswith('/raw/') or target.username or target.password:raise InvalidJob('Untrusted upstream')
        q.mark_stream(job,lease)
        headers={}
        if self.headers.get('Range'):
            if not re.fullmatch(r'bytes=\d*-\d*',self.headers['Range']):raise InvalidJob('Invalid range')
            headers['Range']=self.headers['Range']
        connection=http.client.HTTPConnection(target.hostname,target.port or 80,timeout=60)
        stop=threading.Event()
        def watch():
            while not stop.wait(.5):
                disconnected=False
                try:
                    if select.select([self.connection],[],[],0)[0]:
                        # TLS socket peek is unavailable: readability without
                        # an HTTP pipelined request means this stream ended.
                        disconnected=True
                    if not q.source(job,lease):disconnected=True
                except OSError:disconnected=True
                if disconnected:
                    try:
                        if connection.sock:connection.sock.shutdown(socket.SHUT_RDWR)
                    except OSError:pass
                    connection.close();return
        threading.Thread(target=watch,daemon=True).start()
        try:
            connection.request(self.command,target.path+'?'+target.query,headers=headers)
            response=connection.getresponse()
            self.send_response(response.status)
            for name in ('Content-Type','Content-Length','Content-Range','Accept-Ranges'):
                if response.getheader(name):self.send_header(name,response.getheader(name))
            self.send_header('Connection','close');self.end_headers()
            if self.command!='HEAD':
                while True:
                    part=response.read1(65536)
                    if not part:break
                    self.wfile.write(part)
        finally:stop.set();connection.close()

def main():
    p=argparse.ArgumentParser();p.add_argument('--config',required=True);args=p.parse_args()
    cfg=json.loads(Path(args.config).read_text());creds=Path(os.environ['CREDENTIALS_DIRECTORY'])
    q=DecodeJobs(cfg['queue_path']);q.initialize()
    server=Server((cfg['bind'],cfg['port']),Handler);server.jobs=q;server.allowed=set(cfg['allowed_clients']);server.bridge=cfg['bridge_url']
    server.slots=threading.BoundedSemaphore(12)
    server.auth=hashlib.sha256(('Bearer '+(creds/'verification-token').read_text().strip()).encode()).digest()
    ctx=ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER);ctx.minimum_version=ssl.TLSVersion.TLSv1_2
    ctx.load_cert_chain(creds/'tls-cert',creds/'tls-key');server.context=ctx
    print('Verification HTTPS gateway ready',flush=True);server.serve_forever()

if __name__=='__main__':main()
