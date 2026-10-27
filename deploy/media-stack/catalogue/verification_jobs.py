"""Separate durable decode RPCs. Only the trusted server commits v5 source proofs."""
from contextlib import contextmanager
import hashlib
import json
from pathlib import Path
import re
import sqlite3
import time
import uuid
from urllib.parse import urlsplit
from verification_media import validate_recipe

PROTOCOL = 'home-media-verification/1'
PREFIX = '/v1/verification-worker'
PHASES = frozenset(('video probe','audio conversion','conversion probe','sample decode'))
REASONS = frozenset(('interrupted','resource','transport','decoder','validation'))
CLIENT_TABLES = ('client','second_client','third_client','fourth_client')

class InvalidJob(ValueError): pass
class WorkerWait(RuntimeError): pass

class DecodeJobs:
    def __init__(self,path): self.path=Path(path)
    @contextmanager
    def db(self,write=False):
        c=sqlite3.connect(self.path,timeout=3);c.row_factory=sqlite3.Row
        try:
            if write:c.execute('BEGIN IMMEDIATE')
            yield c;c.commit()
        except BaseException:c.rollback();raise
        finally:c.close()
    def initialize(self):
        self.path.parent.mkdir(parents=True,exist_ok=True);self.path.touch(mode=0o660,exist_ok=True);self.path.chmod(0o660)
        with self.db() as c:
            c.execute('PRAGMA journal_mode=WAL')
            c.executescript('''CREATE TABLE IF NOT EXISTS control(key TEXT PRIMARY KEY,value TEXT);
            CREATE TABLE IF NOT EXISTS client(id INTEGER PRIMARY KEY CHECK(id=1),worker TEXT,seen REAL DEFAULT 0);
            INSERT OR IGNORE INTO client(id) VALUES(1);
            CREATE TABLE IF NOT EXISTS second_client(id INTEGER PRIMARY KEY CHECK(id=2),worker TEXT,seen REAL DEFAULT 0);
            INSERT OR IGNORE INTO second_client(id) VALUES(2);
            CREATE TABLE IF NOT EXISTS third_client(id INTEGER PRIMARY KEY CHECK(id=3),worker TEXT,seen REAL DEFAULT 0);
            INSERT OR IGNORE INTO third_client(id) VALUES(3);
            CREATE TABLE IF NOT EXISTS fourth_client(id INTEGER PRIMARY KEY CHECK(id=4),worker TEXT,seen REAL DEFAULT 0);
            INSERT OR IGNORE INTO fourth_client(id) VALUES(4);
            CREATE TABLE IF NOT EXISTS decode_jobs(id TEXT PRIMARY KEY,key TEXT,sid TEXT,operation TEXT,params TEXT,url TEXT,
              state TEXT,created REAL,controller REAL,deadline REAL,worker TEXT,lease TEXT,expires REAL DEFAULT 0,result TEXT,result_id TEXT,result_hash TEXT);
            CREATE INDEX IF NOT EXISTS decode_due ON decode_jobs(state,created);
            CREATE TABLE IF NOT EXISTS totals(key TEXT PRIMARY KEY,value INTEGER NOT NULL DEFAULT 0);''')
            if 'stream_started' not in {r[1] for r in c.execute('PRAGMA table_info(decode_jobs)')}:c.execute('ALTER TABLE decode_jobs ADD COLUMN stream_started REAL DEFAULT 0')
            columns={r[1] for r in c.execute('PRAGMA table_info(decode_jobs)')}
            for name,definition in (('phase',"TEXT DEFAULT ''"),('phase_at','REAL DEFAULT 0'),
                                    ('outcome',"TEXT DEFAULT ''"),('reason_code',"TEXT DEFAULT ''"),('finished','REAL DEFAULT 0')):
                if name not in columns:c.execute('ALTER TABLE decode_jobs ADD COLUMN '+name+' '+definition)
            c.execute("INSERT OR IGNORE INTO control VALUES('enabled','false')")
            c.execute("INSERT OR IGNORE INTO control VALUES('generation',?)",(json.dumps(uuid.uuid4().hex),))
    def set(self,key,value):
        with self.db(True) as c:c.execute('INSERT OR REPLACE INTO control VALUES (?,?)',(key,json.dumps(value)))
    @staticmethod
    def setting(c,key,default=None):
        r=c.execute('SELECT value FROM control WHERE key=?',(key,)).fetchone();return json.loads(r[0]) if r else default
    def status(self):
        now=time.time()
        with self.db() as c:
            seen=[row[0] for table in CLIENT_TABLES for row in c.execute('SELECT seen FROM '+table)]
            online=sum(value>now-45 for value in seen)
            return {'protocol':PROTOCOL,'enabled':self.setting(c,'enabled',False),'reason':self.setting(c,'reason','Awaiting memory gate / Windows pilot'),
                    'windows_seen':max(seen,default=0), 'windows_online':online>0,'windows_slots_online':online,
                    'windows_capacity':max(1,min(4,int(self.setting(c,'max_windows_jobs',1)))),
                    'activity':self.setting(c,'activity',{}),'backlog':self.setting(c,'backlog',{}),
                    'jobs':dict(c.execute('SELECT state,COUNT(*) FROM decode_jobs GROUP BY state').fetchall()),
                    'totals':dict(c.execute('SELECT key,value FROM totals').fetchall())}
    def seen(self,worker):
        if not isinstance(worker,str) or not re.fullmatch('[a-zA-Z0-9_.:-]{1,128}',worker):raise InvalidJob('Invalid worker')
        with self.db(True) as c:
            workers={table:c.execute('SELECT worker FROM '+table).fetchone()[0] for table in CLIENT_TABLES}
            table=next((name for name,value in workers.items() if value==worker),None)
            if table is None:table=next((name for name,value in workers.items() if value is None),None)
            if table is None:raise InvalidJob('Four enrolled workers already exist')
            c.execute('UPDATE '+table+' SET worker=?,seen=?',(worker,time.time()))
    def submit(self,key,sid,operation,params,url,seconds):
        validate_recipe(operation,params)
        u=urlsplit(url)
        if u.scheme!='http' or u.hostname is None or not u.path.startswith('/raw/') or len(url)>4096:raise InvalidJob('Invalid trusted source')
        if not re.fullmatch('(movie:tt[0-9]{5,12}|series:tt[0-9]{5,12}:[0-9]+:[0-9]+)',key) or not re.fullmatch('[a-f0-9]{64}',sid):raise InvalidJob('Invalid source identity')
        now=time.time();job=uuid.uuid4().hex
        with self.db(True) as c:
            seen=max(c.execute('SELECT seen FROM '+table).fetchone()[0] for table in CLIENT_TABLES)
            if not self.setting(c,'enabled',False) or seen<now-45:raise WorkerWait('Windows verification worker offline or gated')
            c.execute("DELETE FROM decode_jobs WHERE created<? AND state IN ('consumed','cancelled')",(now-7*86400,))
            active=c.execute("SELECT COUNT(*) FROM decode_jobs WHERE state IN ('pending','leased') AND controller>?",(now,)).fetchone()[0]
            if active>=max(1,min(4,int(self.setting(c,'max_windows_jobs',1)))):raise WorkerWait('Windows decode lanes busy')
            if c.execute("SELECT COUNT(*) FROM decode_jobs WHERE state IN ('pending','leased','result')").fetchone()[0]>=16:raise WorkerWait('Decode work capacity reached')
            c.execute('INSERT INTO decode_jobs(id,key,sid,operation,params,url,state,created,controller,deadline,phase,phase_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)',
                      (job,key,sid,operation,json.dumps(params),url,'pending',now,now+30,now+min(480,max(1,seconds)),'queued',now))
        return job
    def claim(self,worker):
        self.seen(worker);now=time.time()
        with self.db(True) as c:
            c.execute("UPDATE decode_jobs SET state='cancelled' WHERE state IN ('pending','leased') AND (controller<? OR deadline<?)",(now,now))
            if not self.setting(c,'enabled',False):return None
            old=c.execute("SELECT * FROM decode_jobs WHERE state='leased' AND worker=? AND expires>?",(worker,now)).fetchone()
            if old:return self.public(old)
            # An expired lease is fenced; its controller must restart this operation.
            c.execute("UPDATE decode_jobs SET state='cancelled' WHERE state='leased' AND expires<=?",(now,))
            r=c.execute("SELECT * FROM decode_jobs WHERE state='pending' ORDER BY created LIMIT 1").fetchone()
            if not r:return None
            lease=uuid.uuid4().hex
            c.execute("UPDATE decode_jobs SET state='leased',worker=?,lease=?,expires=?,phase='claimed',phase_at=? WHERE id=?",(worker,lease,now+180,now,r['id']))
            return self.public(c.execute('SELECT * FROM decode_jobs WHERE id=?',(r['id'],)).fetchone())
    def public(self,r):
        return {'job_id':r['id'],'key':r['key'],'source_id':r['sid'],'operation':r['operation'],'params':json.loads(r['params']),
                'lease_id':r['lease'],'deadline':r['deadline'],'generation':self.generation(),
                'stream_path':PREFIX+'/stream/'+r['id']+'/'+r['lease']}
    def generation(self):
        with self.db() as c:return self.setting(c,'generation')
    def owned(self,c,worker,task):
        now=time.time()
        if task.get('generation')!=self.setting(c,'generation'):return None
        return c.execute("SELECT * FROM decode_jobs WHERE id=? AND lease=? AND worker=? AND state='leased' AND expires>? AND controller>? AND deadline>?",
                         (task.get('job_id'),task.get('lease_id'),worker,now,now,now)).fetchone()
    def renew(self,worker,task,phase=None):
        if phase is not None and phase not in PHASES:raise InvalidJob('Invalid progress phase')
        self.seen(worker)
        with self.db(True) as c:
            r=self.owned(c,worker,task)
            if r:
                now=time.time()
                if phase and phase!=r['phase']:
                    c.execute('UPDATE decode_jobs SET expires=?,phase=?,phase_at=? WHERE id=?',(now+180,phase,now,r['id']))
                else:c.execute('UPDATE decode_jobs SET expires=? WHERE id=?',(now+180,r['id']))
            return bool(r)
    def mark_stream(self,job,lease):
        with self.db(True) as c:
            c.execute("UPDATE decode_jobs SET stream_started=? WHERE id=? AND lease=? AND state='leased'",(time.time(),job,lease))
    def stream_started(self,job):
        with self.db() as c:
            r=c.execute('SELECT stream_started FROM decode_jobs WHERE id=?',(job,)).fetchone()
            return bool(r and r[0])
    def source(self,job,lease):
        with self.db() as c:
            now=time.time();r=c.execute("SELECT * FROM decode_jobs WHERE id=? AND lease=? AND state='leased' AND expires>? AND controller>? AND deadline>?",(job,lease,now,now,now)).fetchone()
            return r['url'] if r else None
    def receive(self,worker,task,result):
        if not isinstance(result,dict) or not re.fullmatch('[a-f0-9]{32}',str(result.get('result_id',''))) or result.get('status') not in ('ok','checkpoint','decode_failure'):raise InvalidJob('Invalid result')
        reason=result.get('reason','')
        if reason and (reason not in REASONS or result['status']=='ok'):raise InvalidJob('Invalid result reason')
        raw=json.dumps(result,sort_keys=True,separators=(',',':'),allow_nan=False)
        if len(raw.encode())>2*1024*1024:raise InvalidJob('Result too large')
        digest=hashlib.sha256(raw.encode()).hexdigest();self.seen(worker)
        with self.db(True) as c:
            old=c.execute('SELECT * FROM decode_jobs WHERE id=?',(task.get('job_id'),)).fetchone()
            if old and old['result_id']==result['result_id']:
                if old['result_hash']!=digest or old['worker']!=worker or old['lease']!=task.get('lease_id'):raise InvalidJob('Conflicting replay')
                return 'duplicate'
            r=self.owned(c,worker,task)
            if not r:return 'stale'
            c.execute("UPDATE decode_jobs SET state='result',result=?,result_id=?,result_hash=?,outcome=?,reason_code=?,finished=? WHERE id=?",
                      (raw,result['result_id'],digest,result['status'],reason,time.time(),r['id']))
            c.execute("INSERT INTO totals VALUES('windows_operations',1) ON CONFLICT(key) DO UPDATE SET value=value+1")
            return 'accepted'
    def poll(self,job):
        with self.db(True) as c:
            r=c.execute('SELECT * FROM decode_jobs WHERE id=?',(job,)).fetchone()
            if not r:return 'cancelled',None
            if r['state'] not in ('cancelled','consumed'):c.execute('UPDATE decode_jobs SET controller=? WHERE id=?',(time.time()+30,job))
            return r['state'],json.loads(r['result']) if r['result'] else None
    def finish(self,job,consumed=False):
        with self.db(True) as c:c.execute("UPDATE decode_jobs SET state=?,url='',params='{}',result=NULL,outcome=CASE WHEN outcome='' THEN ? ELSE outcome END,finished=? WHERE id=?",
                                       ('consumed' if consumed else 'cancelled','ok' if consumed else 'cancelled',time.time(),job))
    def count(self,label):
        with self.db(True) as c:c.execute('INSERT INTO totals VALUES(?,1) ON CONFLICT(key) DO UPDATE SET value=value+1',(label,))
