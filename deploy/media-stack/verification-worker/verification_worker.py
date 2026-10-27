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

def archive_stale_result(folder,task,result):
    quarantine=folder/'quarantine'
    quarantine.mkdir(exist_ok=True)
    archive=quarantine/(result['result_id']+'.json')
    if not archive.exists():
        archive.write_text(json.dumps({'task':task,'result':result}))

class API:
    def __init__(self,config):
        self.origin=urlsplit(config['server_url']);self.token=config['token'];self.pin=config['tls_sha256'].replace(':','').lower()
        if self.origin.scheme!='https' or not self.origin.hostname or self.origin.path not in ('','/') or self.origin.query or self.origin.fragment or self.origin.username or self.origin.password:raise ValueError('Invalid HTTPS origin')
        if not re.fullmatch('[a-f0-9]{64}',self.pin) or not re.fullmatch('[A-Za-z0-9_-]{32,256}',self.token):raise ValueError('Invalid worker connection')
    def connection(self):
        ctx=ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT);ctx.check_hostname=False;ctx.verify_mode=ssl.CERT_NONE;ctx.minimum_version=ssl.TLSVersion.TLSv1_2
        c=http.client.HTTPSConnection(self.origin.hostname,self.origin.port or 443,timeout=60,context=ctx);c.connect()
        if not hmac.compare_digest(hashlib.sha256(c.sock.getpeercert(binary_form=True)).hexdigest(),self.pin):c.close();raise TransportWait('TLS fingerprint changed')
        return c
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

def run_task(api,worker,task,status):
    validate_recipe(task['operation'],task['params'])
    if not re.fullmatch('[a-f0-9]{32}',task['job_id']) or task['stream_path']!=PREFIX+'/stream/'+task['job_id']+'/'+task['lease_id']:raise ValueError('Invalid task stream')
    cancel=threading.Event();proxy=ProxyServer(('127.0.0.1',0),Proxy);proxy.api=api;proxy.task=task;proxy.cancel=cancel
    threading.Thread(target=proxy.serve_forever,daemon=True).start()
    current_phase={'value':None}
    def renew():
        while not cancel.wait(10):
            try:
                body={'worker_id':worker,'task':task}
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
        def status(message):
            summary.update(message=message,last_update=time.time())
            temporary=folder/'status.tmp';temporary.write_text(json.dumps(summary,indent=2));temporary.replace(folder/'status.json')
            LOG.info('%s',message)
        while not STOP.is_set():
            try:
                row=db.execute('SELECT task,result FROM current WHERE id=1').fetchone()
                if not row:
                    reply=api.request('/claim',{'worker_id':worker});task=reply.get('task')
                    server_status=reply.get('status',{});summary['server']=server_status
                    if not task:
                        status('Waiting for work' if server_status.get('enabled') else 'Waiting: '+server_status.get('reason','server gate'))
                        STOP.wait(10);continue
                    with db:db.execute('INSERT INTO current VALUES(1,?,NULL)',(json.dumps(task),))
                    row=(json.dumps(task),None)
                task=json.loads(row[0]);result=json.loads(row[1]) if row[1] else None
                summary['title_key']=task['key'];summary['operation']=task['operation']
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
                    summary['completed_operations']=completed;status('Server acknowledged '+task['operation']+' ('+result['status']+')')
                elif reply.get('ack')=='stale':
                    try:
                        archive_stale_result(folder,task,result)
                    except OSError:
                        status('Local stale-result archive unavailable; progress retained for retry')
                        STOP.wait(15)
                        continue
                    with db:db.execute('DELETE FROM current WHERE id=1')
                    status('Expired result retained; requesting new work')
                else:raise TransportWait('Result not acknowledged')
            except (TransportWait,OSError,http.client.HTTPException,ValueError,sqlite3.Error):
                status('Connection or server unavailable; progress retained for retry');STOP.wait(15)
        status('Stopped; pending results remain on this PC');db.close()

if __name__=='__main__':main()
