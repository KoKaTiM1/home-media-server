"""Optional Windows/Linux Docker decoder. Runs fixed media recipes only."""
import argparse
import contextlib
import fcntl
import hashlib
import hmac
import http.client
from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
import json
import logging
from logging.handlers import RotatingFileHandler
import os
from pathlib import Path
import re
import select
import signal
import socket
import sqlite3
import ssl
import subprocess
import threading
import time
from urllib.parse import urlsplit
import uuid
from verification_jobs import PROTOCOL,PREFIX,PHASES
from verification_media import execute,validate_recipe,validate_result

LOG=logging.getLogger('verification-worker');STOP=threading.Event()
class TransportWait(Exception):pass
class DecodeRejected(Exception):pass

class ProgressLog:
    """Persist every heartbeat, but repeat an unchanged console line at most once/minute."""
    def __init__(self,emit,clock=time.monotonic):
        self.emit=emit;self.clock=clock;self.previous=None;self.last=0
    def write(self,message,job=None):
        now=self.clock();event=(message,job)
        if event!=self.previous or now-self.last>=60:
            self.emit(message);self.previous=event;self.last=now

def waiting_message(server):
    if not server.get('enabled'):return 'Paused by server: '+server.get('reason','verification gate')
    counts=server.get('jobs',{});active=counts.get('leased',0);ready=counts.get('pending',0)
    limit=server.get('windows_capacity',1)
    if active>=limit:return f'Waiting for decode slot: {active}/{limit} active; {ready} ready operations'
    producers=server.get('producer_activity',{}).values()
    preparing=sum(p.get('phase') in ('preparation','source preparation','torrent_resolution','video_probe') for p in producers)
    if preparing:return f'Waiting for server source preparation: {preparing} titles preparing; {ready} ready operations'
    return f'Waiting for ready operation: {active}/{limit} active; {ready} ready operations'

def archive_stale_result(folder,task,result):
    quarantine=folder/'quarantine'
    quarantine.mkdir(exist_ok=True)
    archive=quarantine/(result['result_id']+'.json')
    if not archive.exists():
        archive.write_text(json.dumps({'task':task,'result':result}))

class API:
    def __init__(self,config):
        self.origin=urlsplit(config['server_url']);self.token=config['token'];self.pin=config['tls_sha256'].replace(':','').lower()
        self.origins=[self.origin]
        fallback=config.get('fallback_server_urls',[])
        if not isinstance(fallback,list) or any(not isinstance(value,str) for value in fallback):raise ValueError('Invalid fallback origins')
        for value in fallback:
            candidate=urlsplit(value)
            if candidate.scheme!='https' or not candidate.hostname or candidate.path not in ('','/') or candidate.query or candidate.fragment or candidate.username or candidate.password:raise ValueError('Invalid HTTPS fallback origin')
            if candidate not in self.origins:self.origins.append(candidate)
        if self.origin.scheme!='https' or not self.origin.hostname or self.origin.path not in ('','/') or self.origin.query or self.origin.fragment or self.origin.username or self.origin.password:raise ValueError('Invalid HTTPS origin')
        if not re.fullmatch('[a-f0-9]{64}',self.pin) or not re.fullmatch('[A-Za-z0-9_-]{32,256}',self.token):raise ValueError('Invalid worker connection')
    def connection(self):
        ctx=ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT);ctx.check_hostname=False;ctx.verify_mode=ssl.CERT_NONE;ctx.minimum_version=ssl.TLSVersion.TLSv1_2
        for origin in self.origins:
            c=http.client.HTTPSConnection(origin.hostname,origin.port or 443,timeout=5,context=ctx)
            try:
                c.connect()
                if not hmac.compare_digest(hashlib.sha256(c.sock.getpeercert(binary_form=True)).hexdigest(),self.pin):
                    c.close();continue
                c.sock.settimeout(60)
                return c
            except (OSError,ssl.SSLError):
                c.close()
        raise TransportWait('Coordinator unavailable or access denied')
    def request(self,path,body=None):
        c=self.connection()
        try:
            data=json.dumps({'protocol':PROTOCOL,**body},allow_nan=False).encode() if body is not None else None
            c.request('POST' if body is not None else 'GET',PREFIX+path,data,{'Authorization':'Bearer '+self.token,'Content-Type':'application/json'})
            response=c.getresponse();raw=response.read(2*1024*1024+1)
            if response.status!=200 or len(raw)>2*1024*1024:raise TransportWait('Coordinator unavailable or access denied')
            value=json.loads(raw)
            if value.get('protocol')!=PROTOCOL:raise TransportWait('Protocol changed')
            return value
        finally:c.close()

class Proxy(BaseHTTPRequestHandler):
    def log_message(self,*args):pass
    def do_HEAD(self):self.copy_source()
    def do_GET(self):self.copy_source()
    def copy_source(self):
        if self.path!='/source':self.send_error(404);return
        api=self.server.api;connection=None;done=threading.Event()
        try:
            connection=api.connection()
            def watch():
                while not done.wait(.25):
                    if self.server.cancel.is_set() or select.select([self.connection],[],[],0)[0]:
                        try:
                            if connection.sock:connection.sock.shutdown(socket.SHUT_RDWR)
                        except OSError:pass
                        connection.close();return
            threading.Thread(target=watch,daemon=True).start()
            headers={'Authorization':'Bearer '+api.token}
            if self.headers.get('Range'):headers['Range']=self.headers['Range']
            connection.request(self.command,self.server.task['stream_path'],headers=headers)
            response=connection.getresponse()
            self.send_response(response.status)
            for name in ('Content-Type','Content-Length','Content-Range','Accept-Ranges'):
                if response.getheader(name):self.send_header(name,response.getheader(name))
            self.end_headers()
            if self.command!='HEAD':
                while not self.server.cancel.is_set():
                    part=response.read1(65536)
                    if not part:break
                    self.wfile.write(part)
        except (OSError,TransportWait,http.client.HTTPException):pass
        finally:
            done.set()
            if connection:connection.close()

class ProxyServer(ThreadingHTTPServer):daemon_threads=True

_LOCAL_HEALTH={'time':0,'available':False}
def local_torrent_available():
    if time.monotonic()-_LOCAL_HEALTH['time']<15:return _LOCAL_HEALTH['available']
    import urllib.request
    try:
        with urllib.request.urlopen(os.environ.get('LOCAL_TORRSERVER_URL','http://home-media-verification-torrserver:8090')+'/echo',timeout=2) as reply:
            available=reply.status==200
    except (OSError,ValueError):available=False
    _LOCAL_HEALTH.update(time=time.monotonic(),available=available)
    return available


def run_local_task(api,worker,task,status):
    """The child has isolated title state and a fixed local TorrServer endpoint."""
    from verification_title import validate_task
    validate_task(task['params'])
    if not local_torrent_available():return {'status':'checkpoint','reason':'transport'}
    root=Path('/data/title-jobs')/task['job_id'];root.mkdir(parents=True,exist_ok=True)
    payload=root/'payload.json';output=root/'result.json'
    payload.write_text(json.dumps(task['params']))
    phase='torrent resolution';status(phase)
    command=['python','-u','/worker/local_verifier.py','--payload',str(payload),'--state',str(root/'state'),'--output',str(output)]
    # Only sanitized phase files cross from child to the console.
    with subprocess.Popen(command,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,start_new_session=True) as proc:
        last=0
        try:
            while proc.poll() is None:
                if STOP.is_set() or time.time()>=task['deadline']:return {'status':'checkpoint','reason':'interrupted'}
                try:
                    current=json.loads((root/'state/progress.json').read_text()).get('phase')
                    if current in PHASES and current!=phase:phase=current;status(phase)
                except (OSError,ValueError):pass
                if time.monotonic()-last>=10:
                    if not api.request('/renew',{'worker_id':worker,'task':task,'phase':phase,'local_torrent_available':local_torrent_available()})['active']:return {'status':'checkpoint','reason':'interrupted'}
                    last=time.monotonic()
                STOP.wait(.25)
            if proc.returncode or not output.is_file():return {'status':'checkpoint','reason':'transport'}
            result=json.loads(output.read_text())
            if len(json.dumps(result).encode())>2*1024*1024:return {'status':'checkpoint','reason':'validation'}
            return result
        finally:
            if proc.poll() is None:
                import signal
                try:os.killpg(proc.pid,signal.SIGKILL)
                except ProcessLookupError:pass
            proc.wait()


def run_task(api,worker,task,status):
    if task['operation']=='verify_title':return run_local_task(api,worker,task,status)
    validate_recipe(task['operation'],task['params'])
    if not re.fullmatch('[a-f0-9]{32}',task['job_id']) or task['stream_path']!=PREFIX+'/stream/'+task['job_id']+'/'+task['lease_id']:raise ValueError('Invalid task stream')
    cancel=threading.Event();proxy=ProxyServer(('127.0.0.1',0),Proxy);proxy.api=api;proxy.task=task;proxy.cancel=cancel
    threading.Thread(target=proxy.serve_forever,daemon=True).start()
    current_phase={'value':None}
    def renew():
        while not cancel.wait(10):
            try:
                body={'worker_id':worker,'task':task}
                body['local_torrent_available']=local_torrent_available()
                if current_phase['value']:body['phase']=current_phase['value']
                if not api.request('/renew',body)['active']:cancel.set()
            except Exception:cancel.set()
    threading.Thread(target=renew,daemon=True).start()
    def run(command,phase):
        status(phase)
        if phase in PHASES:current_phase['value']=phase
        with subprocess.Popen(command,stdout=subprocess.PIPE,stderr=subprocess.PIPE) as process:
            try:
                while True:
                    if STOP.is_set() or cancel.is_set() or time.time()>=task['deadline']:raise TransportWait('Operation checkpoint')
                    try:
                        out,err=process.communicate(timeout=1)
                        if len(out)>1024*1024:raise TransportWait('Output bound exceeded')
                        if process.returncode<0 or re.search(rb'Cannot allocate memory|Resource temporarily unavailable|pthread_create failed|Connection refused|Connection reset|HTTP error|Server returned|Input/output error',err,re.I):raise TransportWait('Resource or transport checkpoint')
                        if process.returncode or err:raise DecodeRejected('Sample decoder rejected source')
                        return subprocess.CompletedProcess([],0,out,b'')
                    except subprocess.TimeoutExpired:continue
            finally:
                if process.poll() is None:process.kill()
                process.communicate()
    try:
        data=execute(task['operation'],task['params'],'http://127.0.0.1:'+str(proxy.server_port)+'/source',run,'/tmp')
        validate_result(task['operation'],task['params'],data)
        return {'status':'ok','data':data}
    except DecodeRejected:return {'status':'decode_failure','reason':'decoder'}
    except TransportWait as error:
        reason='interrupted' if str(error)=='Operation checkpoint' else 'resource' if str(error)=='Output bound exceeded' else 'transport'
        return {'status':'checkpoint','reason':reason}
    except (OSError,http.client.HTTPException):return {'status':'checkpoint','reason':'transport'}
    except (ValueError,RuntimeError):return {'status':'checkpoint','reason':'validation'}
    finally:cancel.set();proxy.shutdown();proxy.server_close()

def main():
    p=argparse.ArgumentParser();p.add_argument('command',choices=('run','status'));p.add_argument('--config',default='/config/verification.local.json');p.add_argument('--data',default='/data');a=p.parse_args()
    folder=Path(a.data)
    if a.command=='status':
        path=folder/'status.json';print(path.read_text() if path.exists() else 'Verification worker has not started.');return
    folder.mkdir(parents=True,exist_ok=True)
    for sig in (signal.SIGINT,signal.SIGTERM):signal.signal(sig,lambda *_:STOP.set())
    LOG.setLevel(logging.INFO)
    for handler in (logging.StreamHandler(),RotatingFileHandler(folder/'verification.log',maxBytes=1024*1024,backupCount=2)):
        handler.setFormatter(logging.Formatter('%(asctime)s %(message)s'));LOG.addHandler(handler)
    with (folder/'worker.lock').open('a+') as lock:
        fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        db=sqlite3.connect(folder/'worker.sqlite')
        db.execute('CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY,value TEXT)');db.execute('CREATE TABLE IF NOT EXISTS current(id INTEGER PRIMARY KEY,task TEXT,result TEXT)');db.commit()
        def get(key,default=None):
            row=db.execute('SELECT value FROM settings WHERE key=?',(key,)).fetchone();return json.loads(row[0]) if row else default
        def put(key,value):
            with db:db.execute('INSERT OR REPLACE INTO settings VALUES (?,?)',(key,json.dumps(value)))
        worker=get('identity') or 'verify-'+uuid.uuid4().hex;put('identity',worker)
        config=json.loads(Path(a.config).read_text(encoding='utf-8-sig'));api=API(config)
        origin=hashlib.sha256((config['server_url']+api.pin).encode()).hexdigest()
        if get('origin') not in (None,origin):raise ValueError('Use a new data folder for a new server')
        put('origin',origin)
        summary={'role':'actual-media-verification','completed_operations':get('completed',0),'last_update':time.time()}
        progress_log=ProgressLog(lambda message:LOG.info('%s',message))
        def status(message):
            if message in PHASES:
                message+=f" | {summary.get('title_key','')} | worker acknowledged operations: {summary['completed_operations']}"
            summary.update(message=message,last_update=time.time())
            temporary=folder/'status.tmp';temporary.write_text(json.dumps(summary,indent=2));temporary.replace(folder/'status.json')
            progress_log.write(message,summary.get('job_id'))
        while not STOP.is_set():
            try:
                row=db.execute('SELECT task,result FROM current WHERE id=1').fetchone()
                if not row:
                    reply=api.request('/claim',{'worker_id':worker,'wait_seconds':15,'capabilities':['local-torrent-v1'],'local_torrent_available':local_torrent_available()});task=reply.get('task')
                    server_status=reply.get('status',{});summary['server']=server_status
                    totals=server_status.get('totals',{})
                    summary['server_final_titles']={'verified':totals.get('windows_titles_verified',0),'unavailable':totals.get('windows_titles_unavailable',0)}
                    if not task:
                        summary.update(title_key=None,operation=None,job_id=None)
                        finals=summary['server_final_titles']
                        status(waiting_message(server_status)+f" | server final titles: {finals['verified']} verified, {finals['unavailable']} unavailable")
                        STOP.wait(1 if server_status.get('enabled') else 10);continue
                    with db:db.execute('INSERT INTO current VALUES(1,?,NULL)',(json.dumps(task),))
                    row=(json.dumps(task),None)
                task=json.loads(row[0]);result=json.loads(row[1]) if row[1] else None
                summary.update(title_key=task['key'],last_title_key=task['key'],operation=task['operation'],job_id=task['job_id'])
                if result is None:
                    if not api.request('/renew',{'worker_id':worker,'task':task})['active']:result={'status':'checkpoint'}
                    else:result=run_task(api,worker,task,status)
                    result['result_id']=uuid.uuid4().hex
                    with db:db.execute('UPDATE current SET result=? WHERE id=1',(json.dumps(result),))
                reply=api.request('/result',{'worker_id':worker,'task':task,'result':result})
                if reply.get('result_id')!=result['result_id']:raise TransportWait('Acknowledgement mismatch')
                if reply.get('ack') in ('accepted','duplicate'):
                    with db:
                        completed=get('completed',0)+1
                        db.execute('INSERT OR REPLACE INTO settings VALUES (?,?)',('completed',json.dumps(completed)))
                        db.execute('DELETE FROM current WHERE id=1')
                    summary['completed_operations']=completed
                    if task['operation']=='verify_title':
                        import shutil
                        shutil.rmtree(folder/'title-jobs'/task['job_id'],ignore_errors=True)
                    status('Server acknowledged '+task['operation']+' ('+result['status']+'); '+str(completed)+' operations acknowledged on this worker (not final titles)')
                elif reply.get('ack')=='stale':
                    try:
                        archive_stale_result(folder,task,result)
                    except OSError:
                        status('Local stale-result archive unavailable; progress retained for retry')
                        STOP.wait(15)
                        continue
                    with db:db.execute('DELETE FROM current WHERE id=1')
                    if task['operation']=='verify_title':
                        import shutil
                        shutil.rmtree(folder/'title-jobs'/task['job_id'],ignore_errors=True)
                    status('Expired result retained; requesting new work')
                else:raise TransportWait('Result not acknowledged')
            except (TransportWait,OSError,http.client.HTTPException,ValueError,sqlite3.Error):
                status('Connection or server unavailable; progress retained for retry');STOP.wait(15)
        status('Stopped; pending results remain on this PC');db.close()

if __name__=='__main__':main()
