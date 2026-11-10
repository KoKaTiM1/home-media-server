"""Private compact verification history; one writer, idempotent queue receipts."""
import json,re,sqlite3,time
from contextlib import closing
from pathlib import Path

class DiagnosticLedger:
    def __init__(self,path):self.path=Path(path)
    def connect(self):
        self.path.parent.mkdir(parents=True,exist_ok=True)
        c=sqlite3.connect(self.path,timeout=2);c.execute('PRAGMA journal_mode=WAL')
        c.executescript('''CREATE TABLE IF NOT EXISTS receipts(id TEXT PRIMARY KEY,scope TEXT,key TEXT,finished REAL,outcome TEXT,category TEXT,code TEXT);
        CREATE INDEX IF NOT EXISTS receipt_time ON receipts(finished);
        CREATE TABLE IF NOT EXISTS latest(scope TEXT,key TEXT,id TEXT,finished REAL,bucket TEXT,payload TEXT,PRIMARY KEY(scope,key));
        CREATE TABLE IF NOT EXISTS attempts(id TEXT PRIMARY KEY,scope TEXT,key TEXT,finished REAL,payload TEXT);
        CREATE INDEX IF NOT EXISTS attempt_title ON attempts(scope,key,finished DESC);
        CREATE TABLE IF NOT EXISTS reason_counts(scope TEXT,key TEXT,category TEXT,code TEXT,count INTEGER,PRIMARY KEY(scope,key,category,code));
        CREATE TABLE IF NOT EXISTS buckets(bucket TEXT PRIMARY KEY,count INTEGER NOT NULL);''')
        return c
    def ingest(self,events):
        accepted=[]
        with closing(self.connect()) as c, c:
            for ident,raw in events:
                e=json.loads(raw);key=e['key'];scope=e.get('scope','worker_job');finished=e['finished'];bucket='/'.join((scope,e['outcome'],e['category'] or 'none',e['code'] or 'unknown'))
                added=c.execute('INSERT OR IGNORE INTO receipts VALUES (?,?,?,?,?,?,?)',(ident,scope,key,finished,e['outcome'],e['category'],e['code'])).rowcount
                if added:
                    c.execute('INSERT INTO reason_counts VALUES (?,?,?,?,1) ON CONFLICT(scope,key,category,code) DO UPDATE SET count=count+1',(scope,key,e['category'],e['code']))
                    c.execute('INSERT INTO attempts VALUES (?,?,?,?,?)',(ident,scope,key,finished,raw))
                    old=c.execute('SELECT finished,bucket FROM latest WHERE scope=? AND key=?',(scope,key)).fetchone()
                    if not old or finished>=old[0]:
                        if old:c.execute('UPDATE buckets SET count=count-1 WHERE bucket=?',(old[1],))
                        c.execute('INSERT INTO buckets VALUES (?,1) ON CONFLICT(bucket) DO UPDATE SET count=count+1',(bucket,))
                        c.execute('INSERT OR REPLACE INTO latest VALUES (?,?,?,?,?,?)',(scope,key,ident,finished,bucket,raw))
                    c.execute('DELETE FROM attempts WHERE scope=? AND key=? AND id NOT IN (SELECT id FROM attempts WHERE scope=? AND key=? ORDER BY finished DESC,id DESC LIMIT 3)',(scope,key,scope,key))
                accepted.append(ident)
        return accepted

def final_event(row,version,phases,accepted=None):
    """Project trusted queue fields; never copy arbitrary payload/error text."""
    params=json.loads(row['params'] or '{}');result=json.loads(row['result'] or '{}')
    provider=params.get('_discovery_provider',params.get('provider','none'))
    if provider not in ('torrentio','mediafusion','jackett','mixed','other','none'):provider='none'
    diagnostic=result.get('diagnostic',{})
    source_hash=diagnostic.get('source_hash') if isinstance(diagnostic,dict) else None
    candidates=params.get('candidates',[])
    if not (isinstance(source_hash,str) and re.fullmatch('[a-f0-9]{40}',source_hash) and any(isinstance(x,dict) and x.get('infoHash')==source_hash for x in candidates)):source_hash=None
    if accepted is None:accepted=row['state']=='consumed'
    outcome=row['outcome']
    if outcome=='ok':outcome=('ok' if row['operation']=='verify_title' else 'operation_ok') if accepted else 'unaccepted_ok'
    created=row['created'];claimed=row['claimed_at'] or 0;finished=row['finished']
    return {'scope':'worker_job','key':row['key'],'attempt_id':row['id'],'finished':finished,'operation':row['operation'],
            'worker_version':version if isinstance(version,str) and re.fullmatch(r'\d{1,3}\.\d{1,3}\.\d{1,3}',version) else 'legacy',
            'outcome':outcome,'category':row['failure_category'],'code':row['checkpoint_code'] or row['cancel_code'],
            'failure_type':row['failure_type'],'phase':row['phase'],'provider':provider,'source_hash':source_hash,'source_id':row['sid'],
            'execution_budget_seconds':row['execution_seconds'],'execution_profile':'first_sweep' if params.get('execution_policy',{}).get('profile')=='first_sweep' else 'legacy',
            'queue_seconds':round(max(0,(claimed or finished)-created),3),'execution_seconds':round(max(0,finished-claimed),3) if claimed else 0,
            'active_seconds':row['active_seconds'],'lock_wait_seconds':row['lock_wait_seconds'],'phase_seconds':json.loads(row['worker_phase_seconds']) or dict(phases),'phase_timing':'worker_reported' if json.loads(row['worker_phase_seconds']) else 'coordinator_observed'}


def classify_error(error):
    if error is None:return {'category':'none','code':'none','failure_type':''}
    name=type(error).__name__;code=getattr(error,'code',None)
    known={'EmptyTorrentCandidates':('source_unavailable','no_candidates'),'SourceFailure':('source_unavailable','source_failure'),'SourceTooSlow':('speed_rejected','source_too_slow'),'DecodeFailure':('decoder','decode_failure'),'MetadataPending':('metadata_wait','metadata_pending'),'SourceProviderWait':('provider_wait','provider_wait'),'ClientCancelled':('interrupted','client_cancelled'),'PlaybackBusy':('interrupted','playback_busy'),'OperationalError':('database','sqlite_operational'),'TimeoutError':('transport','timeout'),'HTTPError':('provider','upstream_http'),'URLError':('transport','url_transport'),'LocalTitleCheckpoint':(getattr(error,'failure_category','unknown'),'worker_checkpoint'),'Checkpoint':('transport','checkpoint')}
    category,fallback=known.get(name,('unknown','unknown'))
    override=getattr(error,'failure_category',None)
    if override in ('coordinator','resource','validation'):category=override
    from verification_jobs import DIAGNOSTIC_CODES
    if not isinstance(code,str) or code not in DIAGNOSTIC_CODES:code=fallback
    if name=='SourceProviderWait':
        reason=getattr(error,'reason',None)
        if reason in ('busy','capacity','queue_timeout','cooldown','pacing'):code='provider_'+reason
    if name=='HTTPError' and type(getattr(error,'code',None)) is int and 100<=error.code<=599:code='upstream_http_'+str(error.code)
    if name=='OperationalError' and getattr(error,'sqlite_errorcode',0)&255 in (5,6):code='sqlite_busy'
    return {'category':category,'code':code,'failure_type':name if name in known else 'unknown'}

def export_all(queue_path,catalogue_path=None):
    from verification_jobs import DecodeJobs
    q=DecodeJobs(queue_path);q.initialize();count=q.flush_diagnostics()
    if catalogue_path:
        with closing(sqlite3.connect(catalogue_path,timeout=2)) as c:
            if not c.execute("SELECT 1 FROM sqlite_master WHERE name='verification_attempt_outbox'").fetchone():return count
            events=c.execute('SELECT id,payload FROM verification_attempt_outbox ORDER BY rowid LIMIT 128').fetchall()
            if events:
                imported=DiagnosticLedger(Path(queue_path).with_name('verification-diagnostics.sqlite')).ingest(events)
                with c:c.executemany('DELETE FROM verification_attempt_outbox WHERE id=?',[(x,) for x in imported])
                count+=len(imported)
    return count

if __name__=='__main__':
    import argparse
    p=argparse.ArgumentParser();p.add_argument('--queue',required=True);p.add_argument('--catalogue');args=p.parse_args()
    print('Diagnostic receipts exported:',export_all(args.queue,args.catalogue))
