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
PHASES = frozenset(('video probe','audio conversion','conversion probe','sample decode','torrent resolution','speed verification','title validation','server fallback','waiting for backend lock'))
REASONS = frozenset(('interrupted','resource','transport','decoder','validation'))
DIAGNOSTIC_CODES = frozenset(('preparation_lock_busy','probe_stalled','decode_stalled','metadata_pending','preparation_budget','subprocess_timeout','range_mismatch','speed_window_timeout','speed_range_truncated','speed_startup_timeout','speed_read_timeout','speed_throughput_low','speed_sustained_low','missing_media_size','cache_attribution','uncached_range_budget','speed_work_budget','local_work_budget','local_output_bound','local_transport','unclassified','ffmpeg_resource','ffmpeg_transport','ffmpeg_io','ffmpeg_decoder_missing','ffmpeg_stream_mapping','ffmpeg_missing_index','ffmpeg_invalid_data','ffmpeg_decode_error','ffmpeg_truncated_input','ffmpeg_signal_exit','ffmpeg_nonzero_exit','ffmpeg_stderr_exit','coordinator_job_ended','coordinator_execution_budget','coordinator_queue_budget','coordinator_database','coordinator_transport','worker_permission','worker_import','worker_local_io','worker_task_invalid','worker_child_exit','worker_result_missing'))
CANCEL_CODES = frozenset(('queue_timeout','execution_timeout','controller_lost','lease_expired','controller_budget','foreground_interrupted','controller_error','receipt_interrupted','navigation_interrupted','playback_interrupted','session_unavailable','title_claim_lost','worker_unavailable','controller_cancelled','controller_database','controller_transport','controller_busy','unclassified'))
MAX_WORKERS = 16
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
        self.path.parent.mkdir(parents=True,exist_ok=True);self.path.touch(mode=0o660,exist_ok=True)
        if self.path.stat().st_mode & 0o777 != 0o660:self.path.chmod(0o660)
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
            c.execute('CREATE TABLE IF NOT EXISTS workers(slot INTEGER PRIMARY KEY,worker TEXT UNIQUE NOT NULL,seen REAL NOT NULL DEFAULT 0)')
            if 'lane' not in {r[1] for r in c.execute('PRAGMA table_info(workers)')}:c.execute('ALTER TABLE workers ADD COLUMN lane INTEGER')
            c.execute('CREATE UNIQUE INDEX IF NOT EXISTS workers_lane ON workers(lane)')
            if not self.setting(c,'registry_migrated',False):
                for slot,table in enumerate(CLIENT_TABLES,1):
                    old=c.execute('SELECT worker,seen FROM '+table).fetchone()
                    if old and old[0]:c.execute('INSERT OR IGNORE INTO workers(slot,worker,seen) VALUES (?,?,?)',(slot,*old))
                c.execute("INSERT OR REPLACE INTO control VALUES ('registry_migrated','true')")
            if 'stream_started' not in {r[1] for r in c.execute('PRAGMA table_info(decode_jobs)')}:c.execute('ALTER TABLE decode_jobs ADD COLUMN stream_started REAL DEFAULT 0')
            columns={r[1] for r in c.execute('PRAGMA table_info(decode_jobs)')}
            for name,definition in (('phase',"TEXT DEFAULT ''"),('phase_at','REAL DEFAULT 0'),
                                    ('outcome',"TEXT DEFAULT ''"),('reason_code',"TEXT DEFAULT ''"),('finished','REAL DEFAULT 0'),('failure_category',"TEXT DEFAULT ''"),('failure_type',"TEXT DEFAULT ''"),('checkpoint_code',"TEXT DEFAULT ''"),('cancel_code',"TEXT DEFAULT ''"),('claimed_at','REAL DEFAULT 0'),('execution_seconds','REAL DEFAULT 0'),('lock_wait_seconds','REAL DEFAULT 0'),('active_seconds','REAL DEFAULT 0'),('worker_phase_seconds',"TEXT DEFAULT '{}'"),('diagnostic_queued','INTEGER DEFAULT 0'),('client_version',"TEXT DEFAULT 'legacy'")):
                if name not in columns:c.execute('ALTER TABLE decode_jobs ADD COLUMN '+name+' '+definition)
            c.execute("INSERT OR IGNORE INTO control VALUES('enabled','false')")
            c.execute('CREATE INDEX IF NOT EXISTS diagnostic_unqueued ON decode_jobs(diagnostic_queued,finished)')
            c.execute('CREATE TABLE IF NOT EXISTS diagnostic_outbox(id TEXT PRIMARY KEY,payload TEXT NOT NULL)')
            c.execute('CREATE TABLE IF NOT EXISTS decode_phase_times(job TEXT,phase TEXT,seconds REAL NOT NULL,PRIMARY KEY(job,phase))')
            c.execute("INSERT OR IGNORE INTO control VALUES('generation',?)",(json.dumps(uuid.uuid4().hex),))
    def set(self,key,value):
        with self.db(True) as c:c.execute('INSERT OR REPLACE INTO control VALUES (?,?)',(key,json.dumps(value)))
    @staticmethod
    def setting(c,key,default=None):
        r=c.execute('SELECT value FROM control WHERE key=?',(key,)).fetchone();return json.loads(r[0]) if r else default
    def status(self):
        now=time.time()
        with self.db() as c:
            seen=[r[0] for r in c.execute('SELECT seen FROM workers')]
            online=sum(value>now-45 for value in seen)
            capacity=max(1,min(MAX_WORKERS,int(self.setting(c,'max_windows_jobs',1))))
            producers={k.removeprefix('producer:'):json.loads(v) for k,v in c.execute("SELECT key,value FROM control WHERE key LIKE 'producer:%'")}
            producers={k:v for k,v in producers.items() if v.get('updated',0)>now-45}
            local_backends=sum(self.setting(c,'backend:'+r[0],{}).get('available',False) and self.setting(c,'backend:'+r[0],{}).get('reported',0)>now-45 and r[1]>now-45
                               for r in c.execute('SELECT worker,seen FROM workers') if r[0])
            backend_ids={self.setting(c,'backend:'+r[0],{}).get('id','legacy-1') for r in c.execute('SELECT worker,seen FROM workers') if r[1]>now-45 and self.setting(c,'backend:'+r[0],{}).get('available') and self.setting(c,'backend:'+r[0],{}).get('reported',0)>now-45}
            return {'desired_workers':self.setting(c,'requested_windows_jobs',capacity),'desired_torrservers':self.setting(c,'requested_torrservers',1),'healthy_torrservers':len(backend_ids),'client_versions':{version:sum(1 for w,t in c.execute('SELECT worker,seen FROM workers') if t>now-45 and self.setting(c,'client_version:'+w,'legacy')==version) for version in {self.setting(c,'client_version:'+w,'legacy') for w,t in c.execute('SELECT worker,seen FROM workers') if t>now-45}},'protocol':PROTOCOL,'enabled':self.setting(c,'enabled',False),'reason':self.setting(c,'reason','Awaiting memory gate / Windows pilot'),
                    'windows_seen':max(seen,default=0), 'windows_online':online>0,'windows_slots_online':online,
                    'windows_capacity':capacity,'windows_queue_capacity':self.queue_capacity(c),
                    'producer_activity':producers,
                    'activity':self.setting(c,'activity',{}),'backlog':self.setting(c,'backlog',{}),
                    'jobs':dict(c.execute('SELECT state,COUNT(*) FROM decode_jobs GROUP BY state').fetchall()),
                    'local_torrent_enabled':self.setting(c,'local_torrent_enabled',False),
                    'local_torrent_permitted':self.setting(c,'local_torrent_permitted',False),
                    'local_torrent_continuous':self.setting(c,'local_torrent_continuous',False),
                    'local_torrent_reason':self.setting(c,'local_torrent_reason','Awaiting Windows-local resource gate'),
                    'local_torrent_backends':local_backends,
                    'local_torrent_pilot_remaining':self.setting(c,'local_torrent_pilot_remaining',0),
                    'totals':dict(c.execute('SELECT key,value FROM totals').fetchall())}
    def queue_capacity(self,c):
        capacity=max(1,min(MAX_WORKERS,int(self.setting(c,'max_windows_jobs',1))))
        return min(32,capacity*max(1,min(8,int(self.setting(c,'prefetch_per_lane',1)))))
    def producer(self,slot,key,phase,provider='none'):
        if slot not in range(1,33) or not re.fullmatch('(movie:tt[0-9]{5,12}|series:tt[0-9]{5,12}:[0-9]+:[0-9]+)',key):raise InvalidJob('Invalid producer')
        if phase not in ('source preparation','torrent_resolution','waiting for source metadata','video_probe','preparation',
                         'waiting for decoder','waiting for speed check','speed verification','probe','sample','title validation','finished','windows local torrent','server fallback'):
            raise InvalidJob('Invalid producer phase')
        if provider not in ('torrentio','mediafusion','jackett','mixed','other','none'):raise InvalidJob('Invalid source provider')
        self.set('producer:'+str(slot),{'key':key,'phase':phase,'provider':provider,'updated':time.time()})
    def setting_read(self,key,default=None):
        with self.db() as c:return self.setting(c,key,default)
    def backend_health(self,worker,available,backend_id=None):
        ident=backend_id if isinstance(backend_id,str) and re.fullmatch(r'torrent-[1-8]',backend_id) else 'legacy-1'
        if type(available) is bool:self.set('backend:'+worker,{'available':available,'reported':time.time(),'id':ident})
    def local_workers(self):
        with self.db() as c:
            return sum(bool(self.setting(c,'capability:'+r[0],{}).get('local-torrent-v1')) and self.setting(c,'backend:'+r[0],{}).get('available',False) and self.setting(c,'backend:'+r[0],{}).get('reported',0)>time.time()-45 and r[1]>time.time()-45
                       for r in c.execute('SELECT worker,seen FROM workers') if r[0])
    def seen(self,worker,lane=None,client_version=None):
        if not isinstance(worker,str) or not re.fullmatch('[a-zA-Z0-9_.:-]{1,128}',worker):raise InvalidJob('Invalid worker')
        if lane is not None and (type(lane) is not int or not 1<=lane<=MAX_WORKERS):raise InvalidJob('Invalid worker lane')
        if client_version is not None and (not isinstance(client_version,str) or not re.fullmatch(r'[0-9]{1,3}\.[0-9]{1,3}\.[0-9]{1,3}',client_version)):raise InvalidJob('Invalid client version')
        with self.db(True) as c:
            if client_version:c.execute('INSERT OR REPLACE INTO control VALUES (?,?)',('client_version:'+worker,json.dumps(client_version)))
            row=c.execute('SELECT slot,lane FROM workers WHERE worker=?',(worker,)).fetchone()
            if lane is not None:
                other=c.execute('SELECT worker FROM workers WHERE lane=?',(lane,)).fetchone()
                if other and other[0]!=worker:raise InvalidJob('Worker lane occupied')
            if row:
                if lane is not None and row[1] is not None and lane!=row[1]:raise InvalidJob('Worker identity lane mismatch')
                c.execute('UPDATE workers SET seen=?,lane=COALESCE(lane,?) WHERE worker=?',(time.time(),lane,worker));return
            used={r[0] for r in c.execute('SELECT slot FROM workers')}
            slot=next((n for n in range(1,MAX_WORKERS+1) if n not in used),None)
            if slot is None:raise InvalidJob('Worker enrollment ceiling reached')
            c.execute('INSERT INTO workers(slot,worker,seen,lane) VALUES (?,?,?,?)',(slot,worker,time.time(),lane))
    def configure(self,workers,torrservers):
        if type(workers) is not int or type(torrservers) is not int or not 1<=workers<=MAX_WORKERS or not 1<=torrservers<=min(8,workers):raise InvalidJob('Invalid worker topology')
        with self.db(True) as c:
            for k,v in (('requested_windows_jobs',workers),('requested_torrservers',torrservers),('max_windows_jobs',workers),('prefetch_per_lane',2)):
                c.execute('INSERT OR REPLACE INTO control VALUES (?,?)',(k,json.dumps(v)))
        return {'workers':workers,'torrservers':torrservers}
    def submit(self,key,sid,operation,params,url,seconds,execution_seconds=0):
        if operation=='verify_title':
            from verification_title import validate_task
            validate_task(params)
            if params['key']!=key or url:raise InvalidJob('Invalid title binding')
        else:validate_recipe(operation,params)
        if type(execution_seconds) is not int or not 0<=execution_seconds<=500 or (execution_seconds and operation!='verify_title'):raise InvalidJob('Invalid execution budget')
        u=urlsplit(url)
        if operation!='verify_title' and (u.scheme!='http' or u.hostname is None or not u.path.startswith('/raw/') or len(url)>4096):raise InvalidJob('Invalid trusted source')
        if not re.fullmatch('(movie:tt[0-9]{5,12}|series:tt[0-9]{5,12}:[0-9]+:[0-9]+)',key) or not re.fullmatch('[a-f0-9]{64}',sid):raise InvalidJob('Invalid source identity')
        now=time.time();job=uuid.uuid4().hex
        with self.db(True) as c:
            seen=c.execute('SELECT COALESCE(MAX(seen),0) FROM workers').fetchone()[0]
            permitted=(self.setting(c,'local_torrent_enabled',False) and self.setting(c,'local_torrent_permitted',False)) if operation=='verify_title' else self.setting(c,'enabled',False)
            if not permitted or seen<now-45:raise WorkerWait('Windows verification worker offline or gated')
            c.execute("DELETE FROM decode_jobs WHERE created<? AND state IN ('consumed','cancelled') AND diagnostic_queued=1",(now-7*86400,))
            active=c.execute("SELECT COUNT(*) FROM decode_jobs WHERE state IN ('pending','leased') AND controller>?",(now,)).fetchone()[0]
            if active>=self.queue_capacity(c):raise WorkerWait('Windows ready queue full')
            # A result remains durable until its originating controller calls
            # finish(). If that controller dies, the retained receipt must not
            # permanently consume live queue capacity; a live controller keeps
            # its result protected by refreshing `controller` from poll().
            live=c.execute("SELECT COUNT(*) FROM decode_jobs WHERE state IN ('pending','leased') OR (state='result' AND controller>?)",(now,)).fetchone()[0]
            if live>=32:raise WorkerWait('Decode work capacity reached')
            if operation=='verify_title':
                continuous=self.setting(c,'local_torrent_continuous',False)
                remaining=self.setting(c,'local_torrent_pilot_remaining',20)
                if (not continuous and remaining<=0) or not self.setting(c,'local_torrent_enabled',False) or not self.setting(c,'local_torrent_permitted',False):raise WorkerWait('Local torrent verification paused')
                if not continuous:c.execute('INSERT OR REPLACE INTO control VALUES (?,?)',('local_torrent_pilot_remaining',json.dumps(remaining-1)))
            c.execute('INSERT INTO decode_jobs(id,key,sid,operation,params,url,state,created,controller,deadline,phase,phase_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)',
                      (job,key,sid,operation,json.dumps(params),url,'pending',now,now+30,now+min(600,max(1,seconds)),'queued',now))
            if execution_seconds:c.execute('UPDATE decode_jobs SET execution_seconds=? WHERE id=?',(execution_seconds,job))
        return job
    def claim(self,worker,capabilities=None):
        self.seen(worker);now=time.time()
        capable=isinstance(capabilities,list) and 'local-torrent-v1' in capabilities
        if capabilities is not None:self.set('capability:'+worker,{'local-torrent-v1':capable})
        with self.db(True) as c:
            c.execute("UPDATE decode_jobs SET state='cancelled',cancel_code=CASE WHEN deadline<? THEN CASE WHEN state='pending' THEN 'queue_timeout' ELSE 'execution_timeout' END ELSE 'controller_lost' END WHERE state IN ('pending','leased') AND (controller<? OR deadline<?)",(now,now,now))
            server_permitted=self.setting(c,'enabled',False)
            local_permitted=self.setting(c,'local_torrent_enabled',False) and self.setting(c,'local_torrent_permitted',False)
            if not server_permitted and not (local_permitted and capable):return None
            old=c.execute("SELECT * FROM decode_jobs WHERE state='leased' AND worker=? AND expires>?",(worker,now)).fetchone()
            if old:return self.public(old)
            slot=c.execute('SELECT COALESCE(lane,slot) FROM workers WHERE worker=?',(worker,)).fetchone()[0]
            if slot>self.setting(c,'requested_windows_jobs',MAX_WORKERS):return None
            # An expired lease is fenced; its controller must restart this operation.
            c.execute("UPDATE decode_jobs SET state='cancelled',cancel_code='lease_expired' WHERE state='leased' AND expires<=?",(now,))
            # Queue capacity and active decode capacity are independent. Extra
            # ready work never lets a fourth client bypass a three-slot gate.
            active=c.execute("SELECT COUNT(*) FROM decode_jobs WHERE state='leased'").fetchone()[0]
            if active>=max(1,min(MAX_WORKERS,int(self.setting(c,'max_windows_jobs',1)))):return None
            r=c.execute("SELECT * FROM decode_jobs WHERE state='pending' AND ((operation='verify_title' AND ?) OR (operation!='verify_title' AND ?)) ORDER BY created LIMIT 1",
                        (int(capable and local_permitted),int(server_permitted))).fetchone()
            if not r:return None
            lease=uuid.uuid4().hex
            c.execute("UPDATE decode_jobs SET state='leased',worker=?,lease=?,expires=?,phase='claimed',phase_at=?,claimed_at=?,client_version=?,deadline=CASE WHEN execution_seconds>0 THEN ?+execution_seconds ELSE deadline END WHERE id=?",(worker,lease,now+180,now,now,self.setting(c,'client_version:'+worker,'legacy'),now,r['id']))
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
                    self.phase_time(c,r,now)
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
        # A Windows client may report which exact source failed the V6 speed
        # check. Retain that hint only when it names a Torrentio candidate in
        # this lease's immutable submitted payload; never trust arbitrary hashes.
        result=dict(result)
        diagnostic=result.get('diagnostic')
        if isinstance(diagnostic,dict) and 'source_hash' in diagnostic:
            diagnostic=dict(diagnostic)
            source_hash=diagnostic.get('source_hash')
            params=task.get('params') if isinstance(task,dict) else None
            candidates=params.get('candidates',[]) if isinstance(params,dict) else []
            matched=(diagnostic.get('category')=='speed_rejected'
                     and isinstance(source_hash,str) and re.fullmatch(r'[a-f0-9]{40}',source_hash)
                     and isinstance(candidates,list)
                     and any(isinstance(candidate,dict)
                             and str(candidate.get('infoHash','')).lower()==source_hash
                             and candidate.get('provider')=='torrentio'
                             for candidate in candidates))
            if not matched:diagnostic.pop('source_hash',None)
            result['diagnostic']=diagnostic
        from source_failure_diagnostics import sanitize as sanitize_failure_evidence
        if isinstance(result.get('diagnostic'),dict):
            d=dict(result['diagnostic']);d.pop('root_cause',None);d.pop('source_failures',None)
            d.update(sanitize_failure_evidence(result['diagnostic']))
            result['diagnostic']=d
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
            diagnostic=result.get('diagnostic',{})
            if isinstance(diagnostic,dict):
                params=json.loads(r['params'] or '{}')
                assigned={x.get('infoHash') for x in params.get('candidates',[]) if isinstance(x,dict)}
                safe=sanitize_failure_evidence(diagnostic,assigned)
                diagnostic=dict(diagnostic);diagnostic.pop('root_cause',None);diagnostic.pop('source_failures',None)
                diagnostic.update(safe);result['diagnostic']=diagnostic
                raw=json.dumps(result,sort_keys=True,separators=(',',':'),allow_nan=False)
            category=diagnostic.get('category','') if isinstance(diagnostic,dict) else ''
            failure_type=diagnostic.get('failure_type','') if isinstance(diagnostic,dict) else ''
            if category not in ('torrent_timeout','stream_stall','speed_rejected','decoder','resource','coordinator','transport','interrupted','validation'):category=''
            if failure_type not in ('TimeoutError','URLError','HTTPError','Checkpoint','SourceStalled','SourceFailure','SourceTooSlow','ResourceCheckpoint','DecodeFailure','ValueError','RuntimeError','OSError','ConnectionError'):failure_type=''
            code=diagnostic.get('code','') if isinstance(diagnostic,dict) else ''
            if code not in DIAGNOSTIC_CODES:code=''
            metrics=result.get('metrics',{})
            def metric(name):
                v=metrics.get(name,0) if isinstance(metrics,dict) else 0
                return float(v) if type(v) in (int,float) and 0<=v<=960 else 0
            phases=metrics.get('phase_seconds',{}) if isinstance(metrics,dict) else {}
            if not (isinstance(phases,dict) and len(phases)<=len(PHASES) and all(k in PHASES and type(v) in (int,float) and 0<=v<=960 for k,v in phases.items()) and sum(phases.values())<=960):phases={}
            c.execute('UPDATE decode_jobs SET worker_phase_seconds=? WHERE id=?',(json.dumps(phases),r['id']))
            c.execute('UPDATE decode_jobs SET checkpoint_code=?,lock_wait_seconds=?,active_seconds=? WHERE id=?',(code,metric('lock_wait_seconds'),metric('active_seconds'),r['id']))
            stage=diagnostic.get('phase') if isinstance(diagnostic,dict) else None
            if stage in PHASES and stage!=r['phase']:
                self.phase_time(c,r,time.time())
                c.execute('UPDATE decode_jobs SET phase=?,phase_at=? WHERE id=?',(stage,time.time(),r['id']))
            c.execute('UPDATE decode_jobs SET failure_category=?,failure_type=? WHERE id=?',(category,failure_type,r['id']))
            c.execute("UPDATE decode_jobs SET state='result',result=?,result_id=?,result_hash=?,outcome=?,reason_code=?,finished=? WHERE id=?",
                      (raw,result['result_id'],digest,result['status'],reason,time.time(),r['id']))
            c.execute("INSERT INTO totals VALUES('windows_operations',1) ON CONFLICT(key) DO UPDATE SET value=value+1")
            return 'accepted'
    def job_clock(self,job):
        with self.db() as c:
            r=c.execute('SELECT created,claimed_at,deadline,execution_seconds FROM decode_jobs WHERE id=?',(job,)).fetchone()
            return dict(r) if r else None
    def poll(self,job):
        with self.db(True) as c:
            r=c.execute('SELECT * FROM decode_jobs WHERE id=?',(job,)).fetchone()
            if not r:return 'cancelled',None
            now=time.time()
            if r['state'] in ('pending','leased') and (r['deadline']<=now or (r['state']=='leased' and r['expires']<=now)):
                code=('queue_timeout' if r['state']=='pending' else 'execution_timeout') if r['deadline']<=now else 'lease_expired'
                c.execute("UPDATE decode_jobs SET state='cancelled',cancel_code=? WHERE id=?",(code,job))
                return 'cancelled',None
            if r['state'] not in ('cancelled','consumed'):c.execute('UPDATE decode_jobs SET controller=? WHERE id=?',(now+30,job))
            return r['state'],json.loads(r['result']) if r['result'] else None
    def finish(self,job,consumed=False,cancel_code='unclassified'):
        if cancel_code not in CANCEL_CODES:cancel_code='unclassified'
        with self.db(True) as c:
            c.execute("UPDATE decode_jobs SET cancel_code=CASE WHEN outcome='' AND cancel_code='' AND ?=0 THEN CASE WHEN deadline<=? THEN CASE WHEN claimed_at=0 THEN 'queue_timeout' ELSE 'execution_timeout' END ELSE ? END ELSE cancel_code END WHERE id=?",(int(consumed),time.time(),cancel_code,job))
            row=c.execute('SELECT * FROM decode_jobs WHERE id=?',(job,)).fetchone()
            if row and (row['state'] not in ('consumed','cancelled') or not row['finished']):
                self.phase_time(c,row,row['finished'] or time.time())
            c.execute("UPDATE decode_jobs SET outcome=CASE WHEN outcome='' THEN ? ELSE outcome END,finished=CASE WHEN finished=0 THEN ? ELSE finished END WHERE id=?",('ok' if consumed else 'cancelled',time.time(),job))
            row=c.execute('SELECT * FROM decode_jobs WHERE id=?',(job,)).fetchone()
            if row:
                from verification_diagnostics import final_event
                phases=c.execute('SELECT phase,seconds FROM decode_phase_times WHERE job=?',(job,)).fetchall()
                event=final_event(row,row['client_version'],phases,consumed)
                c.execute('INSERT OR IGNORE INTO diagnostic_outbox VALUES (?,?)',(job,json.dumps(event,sort_keys=True)))
                c.execute('UPDATE decode_jobs SET diagnostic_queued=1 WHERE id=?',(job,))
            c.execute("UPDATE decode_jobs SET state=?,url='',params='{}',result=NULL,outcome=CASE WHEN outcome='' THEN ? ELSE outcome END,finished=? WHERE id=?",
                                       ('consumed' if consumed else 'cancelled','ok' if consumed else 'cancelled',row['finished'] if row else time.time(),job))
    def phase_time(self,c,row,now):
        if row['phase'] and row['phase_at']:
            seconds=max(0,min(960,now-row['phase_at']))
            c.execute('INSERT INTO decode_phase_times VALUES (?,?,?) ON CONFLICT(job,phase) DO UPDATE SET seconds=seconds+excluded.seconds',(row['id'],row['phase'],seconds))
    def flush_diagnostics(self,limit=128):
        from verification_diagnostics import DiagnosticLedger
        from verification_diagnostics import final_event
        with self.db(True) as c:
            for row in c.execute("SELECT * FROM decode_jobs WHERE diagnostic_queued=0 AND finished>0 AND state IN ('consumed','cancelled') ORDER BY finished LIMIT ?",(limit,)).fetchall():
                event=final_event(row,row['client_version'],[])
                c.execute('INSERT OR IGNORE INTO diagnostic_outbox VALUES (?,?)',(row['id'],json.dumps(event,sort_keys=True)))
                c.execute('UPDATE decode_jobs SET diagnostic_queued=1 WHERE id=?',(row['id'],))
            events=[tuple(r) for r in c.execute('SELECT id,payload FROM diagnostic_outbox ORDER BY rowid LIMIT ?',(limit,))]
        if not events:return 0
        imported=DiagnosticLedger(self.path.with_name('verification-diagnostics.sqlite')).ingest(events)
        with self.db(True) as c:
            c.executemany('DELETE FROM diagnostic_outbox WHERE id=?',[(x,) for x in imported])
            c.executemany('DELETE FROM decode_phase_times WHERE job=?',[(x,) for x in imported])
        return len(imported)
    def count(self,label):
        with self.db(True) as c:c.execute('INSERT INTO totals VALUES(?,1) ON CONFLICT(key) DO UPDATE SET value=value+1',(label,))
