"""Paged discovery, Hebrew identities and managed server downloads."""
import datetime as dt
import csv
from contextlib import closing, contextmanager
import fcntl
import gzip
import hashlib
import json
from pathlib import Path
import re
import shutil
import sqlite3
import threading
import time
import uuid
import urllib.error
import urllib.parse as U
import urllib.request as R
import xml.etree.ElementTree as ET


class Features:
    def __init__(self, runtime):
        self.g=runtime
        self.download_lock=threading.Lock()
        self.count_lock=threading.Lock()
        self.count_cache={}
        self.episode_count_lock=threading.Lock()
        self.episode_count_cache={}
        self.read_model_lock=threading.Lock()

    def db(self):
        conn=self.g['db']()
        conn.execute('CREATE TABLE IF NOT EXISTS discovery (kind TEXT, imdb TEXT, meta TEXT, PRIMARY KEY(kind,imdb))')
        conn.execute('CREATE TABLE IF NOT EXISTS release_dates (kind TEXT, imdb TEXT, released TEXT, PRIMARY KEY(kind,imdb))')
        conn.execute('CREATE TABLE IF NOT EXISTS aliases (imdb TEXT PRIMARY KEY, hebrew TEXT, english TEXT)')
        conn.execute('''CREATE TABLE IF NOT EXISTS localized_metadata (
            kind TEXT NOT NULL, imdb TEXT NOT NULL, title_he TEXT, description_he TEXT,
            source TEXT, state TEXT NOT NULL DEFAULT 'pending', checked_at REAL NOT NULL DEFAULT 0,
            next_attempt REAL NOT NULL DEFAULT 0, nfo_written INTEGER NOT NULL DEFAULT 0,
            refresh_pending INTEGER NOT NULL DEFAULT 0,
            PRIMARY KEY(kind,imdb))''')
        conn.execute('CREATE INDEX IF NOT EXISTS localized_metadata_due ON localized_metadata(state,next_attempt)')
        if 'refresh_pending' not in {row[1] for row in conn.execute('PRAGMA table_info(localized_metadata)')}:
            try:conn.execute('ALTER TABLE localized_metadata ADD COLUMN refresh_pending INTEGER NOT NULL DEFAULT 0')
            except sqlite3.OperationalError:
                if 'refresh_pending' not in {row[1] for row in conn.execute('PRAGMA table_info(localized_metadata)')}:raise
        conn.execute('CREATE TABLE IF NOT EXISTS downloads (key TEXT PRIMARY KEY, value TEXT)')
        conn.execute("CREATE TABLE IF NOT EXISTS validation (key TEXT PRIMARY KEY, state TEXT NOT NULL DEFAULT 'pending', attempts INTEGER NOT NULL DEFAULT 0, next_check REAL NOT NULL DEFAULT 0, checked REAL NOT NULL DEFAULT 0, proof TEXT, reason TEXT, priority INTEGER NOT NULL DEFAULT 0)")
        conn.execute('CREATE TABLE IF NOT EXISTS audio_recheck (key TEXT PRIMARY KEY, completed REAL NOT NULL DEFAULT 0)')
        conn.execute('CREATE INDEX IF NOT EXISTS validation_due ON validation(state,next_check,priority)')
        conn.execute("CREATE INDEX IF NOT EXISTS validation_work ON validation(priority DESC,next_check) WHERE state!='verified'")
        conn.execute('CREATE TABLE IF NOT EXISTS verification_holds(key TEXT PRIMARY KEY,expires REAL NOT NULL,reason TEXT)')
        conn.execute('CREATE TABLE IF NOT EXISTS verification_claims(key TEXT PRIMARY KEY,token TEXT,owner TEXT,expires REAL)')
        conn.execute('CREATE TABLE IF NOT EXISTS metadata_waits(key TEXT PRIMARY KEY,expires REAL NOT NULL)')
        conn.execute('CREATE TABLE IF NOT EXISTS verification_retry_waits(key TEXT PRIMARY KEY,attempts INTEGER NOT NULL,category TEXT NOT NULL,next_retry REAL NOT NULL)')
        conn.execute('CREATE INDEX IF NOT EXISTS metadata_waits_due ON metadata_waits(expires)')
        conn.execute('''CREATE TABLE IF NOT EXISTS verification_sweeps(
            id INTEGER PRIMARY KEY AUTOINCREMENT,started REAL NOT NULL,rowid_highwater INTEGER NOT NULL,
            baseline_total INTEGER NOT NULL,completed REAL NOT NULL DEFAULT 0)''')
        conn.execute('''CREATE TABLE IF NOT EXISTS verification_sweep_attempts(
            sweep_id INTEGER NOT NULL,key TEXT NOT NULL,started REAL NOT NULL,finished REAL NOT NULL DEFAULT 0,
            outcome TEXT NOT NULL DEFAULT 'started',PRIMARY KEY(sweep_id,key))''')
        conn.execute('CREATE INDEX IF NOT EXISTS verification_sweep_attempts_id ON verification_sweep_attempts(sweep_id,outcome)')
        conn.execute('CREATE TABLE IF NOT EXISTS manual_rechecks(key TEXT PRIMARY KEY,requested REAL,request_id TEXT,status TEXT)')
        conn.execute('CREATE TABLE IF NOT EXISTS recheck_requests(id TEXT PRIMARY KEY,requested REAL,count INTEGER)')
        self.ensure_read_model(conn)
        return conn

    def defer_verification(self,key,category):
        with self.db() as conn:
            row=conn.execute('SELECT attempts FROM verification_retry_waits WHERE key=?',(key,)).fetchone()
            attempts=(row[0] if row else 0)+1
            due=time.time()+(300,900,3600,21600)[min(attempts-1,3)]
            conn.execute('INSERT OR REPLACE INTO verification_retry_waits VALUES (?,?,?,?)',(key,attempts,category,due))
            conn.execute('INSERT INTO metadata_waits VALUES (?,?) ON CONFLICT(key) DO UPDATE SET expires=MAX(expires,excluded.expires)',(key,due))
        return due
    def clear_verification_retry(self,key):
        with self.db() as conn:
            conn.execute('DELETE FROM metadata_waits WHERE key=? AND EXISTS(SELECT 1 FROM verification_retry_waits WHERE key=?)',(key,key))
            conn.execute('DELETE FROM verification_retry_waits WHERE key=?',(key,))

    @staticmethod
    def _sweep_eligible_sql(alias='v'):
        return f"(({alias}.state='pending') OR ({alias}.state='unavailable' AND {alias}.next_check<=?) OR ({alias}.state='verified' AND COALESCE(json_extract({alias}.proof,'$.version'),0)<6))"

    def start_verification_sweep(self):
        """Take one durable snapshot of keys eligible for their first full V6 attempt."""
        now=time.time()
        with self.db() as conn:
            columns={r[1] for r in conn.execute('PRAGMA table_info(verification_sweep_attempts)')}
            if 'priority' not in columns:
                conn.execute('BEGIN IMMEDIATE')
                columns={r[1] for r in conn.execute('PRAGMA table_info(verification_sweep_attempts)')}
                for name,kind in (('priority','INTEGER'),('source_version','INTEGER'),('due','REAL')):
                    if name in columns:continue
                    conn.execute(f'ALTER TABLE verification_sweep_attempts ADD COLUMN {name} {kind} NOT NULL DEFAULT 0')
                conn.execute("UPDATE verification_sweep_attempts SET (priority,source_version,due)=(SELECT priority,COALESCE(json_extract(proof,'$.version'),0),next_check FROM validation WHERE validation.key=verification_sweep_attempts.key)")
                conn.commit()
            if not conn.execute("SELECT 1 FROM sqlite_master WHERE name='verification_sweep_order'").fetchone():
                conn.execute('CREATE INDEX IF NOT EXISTS verification_sweep_order ON verification_sweep_attempts(sweep_id,outcome,priority DESC,source_version,due)')
                conn.commit()
            conn.execute("CREATE INDEX IF NOT EXISTS verification_sweep_v5_order ON verification_sweep_attempts(sweep_id,priority DESC,due) WHERE outcome='queued' AND source_version=5")
            if conn.execute('SELECT 1 FROM verification_sweeps LIMIT 1').fetchone():return
            conn.execute('BEGIN IMMEDIATE')
            if conn.execute('SELECT 1 FROM verification_sweeps LIMIT 1').fetchone():return
            high=conn.execute('SELECT COALESCE(MAX(rowid),0) FROM validation').fetchone()[0]
            eligible=self._sweep_eligible_sql('v')
            total=conn.execute(f"SELECT COUNT(*) FROM validation v WHERE v.rowid<=? AND {eligible} AND NOT EXISTS(SELECT 1 FROM verification_audit a WHERE a.key=v.key AND a.reason='speed-v6' AND a.completed>0)",(high,now)).fetchone()[0]
            cur=conn.execute('INSERT INTO verification_sweeps(started,rowid_highwater,baseline_total) VALUES (?,?,?)',(now,high,total))
            sweep_id=cur.lastrowid
            # Materialize the starting snapshot once; producers then claim from
            # this indexed queue instead of repeatedly scanning the million-row
            # validation table. Retained checkpoints count as already attempted.
            conn.execute(f"INSERT OR IGNORE INTO verification_sweep_attempts(sweep_id,key,started,outcome,priority,source_version,due) SELECT ?,v.key,?,'queued',v.priority,COALESCE(json_extract(v.proof,'$.version'),0),v.next_check FROM validation v WHERE v.rowid<=? AND {eligible} AND NOT EXISTS(SELECT 1 FROM verification_audit a WHERE a.key=v.key AND a.reason='speed-v6' AND a.completed>0)",(sweep_id,now,high,now))

    def first_sweep_candidate(self):
        """Return an unattempted baseline key, then fresh arrivals, before retries."""
        now=time.time()
        eligible=self._sweep_eligible_sql('v')
        with self.db() as conn:
            sweep=conn.execute('SELECT id,started,rowid_highwater,completed FROM verification_sweeps ORDER BY id DESC LIMIT 1').fetchone()
            if not sweep:return None,False
            sweep_id,started,high,completed=sweep
            if not completed:
                # Reconcile only a small indexed queue window. Global updates
                # and sorting the complete snapshot serialize all producers.
                stale=conn.execute("SELECT 1 FROM verification_sweep_attempts WHERE sweep_id=? AND outcome='started' AND started<? LIMIT 1",(sweep_id,now-600)).fetchone()
                if stale:
                    conn.execute("UPDATE verification_sweep_attempts SET outcome='queued',started=? WHERE sweep_id=? AND outcome='started' AND started<? AND NOT EXISTS(SELECT 1 FROM verification_claims c WHERE c.key=verification_sweep_attempts.key AND c.expires>?)",(now,sweep_id,now-600,now))
                selection="""SELECT x.key,v.state,v.next_check,
                    COALESCE(json_extract(v.proof,'$.version'),0),v.priority,
                    EXISTS(SELECT 1 FROM verification_audit a WHERE a.key=x.key AND a.reason='speed-v6' AND a.completed>0)
                    FROM verification_sweep_attempts x INDEXED BY {index}
                    JOIN validation v ON v.key=x.key
                    WHERE x.sweep_id=? AND x.outcome='queued' {version_filter}
                    AND NOT EXISTS(SELECT 1 FROM verification_claims c WHERE c.key=x.key AND c.expires>?)
                    AND NOT EXISTS(SELECT 1 FROM verification_holds h WHERE h.key=x.key AND h.expires>?)
                    AND NOT EXISTS(WITH RECURSIVE parents(k,pos) AS (SELECT x.key,0 UNION ALL SELECT k,pos+1 FROM parents WHERE pos<length(k)) SELECT 1 FROM parents JOIN metadata_waits m ON m.key=CASE WHEN pos=0 THEN k ELSE substr(k,1,pos-1) END WHERE (pos=0 OR substr(k,pos,1)=':') AND m.expires>?)
                    ORDER BY {ordering} LIMIT 64"""
                parameters=(sweep_id,now,now,now)
                # Keep the V5 audit ahead of discovery without scanning the
                # million-key queue or rewriting its saved priority values.
                rows=conn.execute(selection.format(index='verification_sweep_v5_order',version_filter='AND x.source_version=5',ordering='x.priority DESC,x.due'),parameters).fetchall()
                if not rows:
                    rows=conn.execute(selection.format(index='verification_sweep_order',version_filter='',ordering='x.priority DESC,x.source_version,x.due'),parameters).fetchall()
                candidates=[]
                for key,state,due,version,priority,audited in rows:
                    if audited or (state=='verified' and version>=6):
                        outcome='passed' if audited else 'deferred'
                    elif state=='unavailable' and due>now:outcome='deferred'
                    else:
                        candidates.append((key,priority,version,due));continue
                    conn.execute("UPDATE verification_sweep_attempts SET outcome=?,finished=? WHERE sweep_id=? AND key=? AND outcome='queued'",(outcome,now,sweep_id,key))
                if candidates:
                    row=min(candidates,key=lambda r:(-r[1],r[2],r[3],r[0]))
                    return (sweep_id,row[0]),True
                queued=conn.execute("SELECT 1 FROM verification_sweep_attempts WHERE sweep_id=? AND outcome='queued' LIMIT 1",(sweep_id,)).fetchone()
                started_count=conn.execute("SELECT 1 FROM verification_sweep_attempts WHERE sweep_id=? AND outcome='started' LIMIT 1",(sweep_id,)).fetchone()
                if queued or started_count:return None,True
                conn.execute('UPDATE verification_sweeps SET completed=? WHERE id=? AND completed=0',(now,sweep_id))
                return None,False
            row=conn.execute(f"""SELECT v.key FROM validation v WHERE v.rowid>? AND {eligible}
                AND NOT EXISTS(SELECT 1 FROM verification_sweep_attempts x WHERE x.sweep_id=? AND x.key=v.key)
                AND NOT EXISTS(SELECT 1 FROM verification_audit a WHERE a.key=v.key AND a.reason='speed-v6' AND a.completed>0)
                AND NOT EXISTS(SELECT 1 FROM verification_claims c WHERE c.key=v.key AND c.expires>?)
                AND NOT EXISTS(SELECT 1 FROM verification_holds h WHERE h.key=v.key AND h.expires>?)
                AND NOT EXISTS(SELECT 1 FROM metadata_waits m WHERE m.expires>? AND (v.key=m.key OR v.key LIKE m.key||':%'))
                ORDER BY v.priority DESC,COALESCE(json_extract(v.proof,'$.version'),0),v.next_check,v.rowid LIMIT 1""",(high,now,sweep_id,now,now,now)).fetchone()
            if row:return (sweep_id,row[0]),True
            return None,False

    def mark_first_sweep_attempt(self,key):
        now=time.time()
        with self.db() as conn:
            sweep=conn.execute('SELECT id,rowid_highwater FROM verification_sweeps ORDER BY id DESC LIMIT 1').fetchone()
            if not sweep:return False
            sweep_id,high=sweep
            row=conn.execute('SELECT rowid FROM validation WHERE key=?',(key,)).fetchone()
            latest=conn.execute('SELECT completed FROM verification_sweeps WHERE id=?',(sweep_id,)).fetchone()
            if not row:return False
            if (not latest or (row[0]<=high and latest[0]) or (row[0]>high and not latest[0])):return False
            if row[0]>high:
                eligible=self._sweep_eligible_sql('v')
                if not conn.execute(f"SELECT 1 FROM validation v WHERE v.key=? AND {eligible} AND NOT EXISTS(SELECT 1 FROM verification_audit a WHERE a.key=v.key AND a.reason='speed-v6' AND a.completed>0)",(key,time.time())).fetchone():return False
            if row[0]<=high:
                cur=conn.execute("UPDATE verification_sweep_attempts SET started=?,outcome='started' WHERE sweep_id=? AND key=? AND outcome='queued'",(now,sweep_id,key))
            else:
                cur=conn.execute("INSERT OR IGNORE INTO verification_sweep_attempts(sweep_id,key,started,outcome) VALUES (?,?,?,'started')",(sweep_id,key,now))
            return bool(cur.rowcount)

    def finish_first_sweep_attempt(self,key,outcome):
        if outcome not in ('passed','failed','deferred'):outcome='deferred'
        with self.db() as conn:
            conn.execute("UPDATE verification_sweep_attempts SET finished=?,outcome=? WHERE key=? AND outcome='started' AND sweep_id=(SELECT MAX(id) FROM verification_sweeps)",(time.time(),outcome,key))

    def first_sweep_progress(self):
        with self.db() as conn:
            row=conn.execute('SELECT id,started,rowid_highwater,baseline_total,completed FROM verification_sweeps ORDER BY id DESC LIMIT 1').fetchone()
            if not row:return None
            counts=dict(conn.execute("SELECT x.outcome,COUNT(*) FROM verification_sweep_attempts x JOIN validation v ON v.key=x.key WHERE x.sweep_id=? AND v.rowid<=? AND x.outcome IN ('passed','failed','deferred') GROUP BY x.outcome",(row[0],row[2])).fetchall())
            baseline=sum(counts.values())
            return {'started':row[1],'total':row[3],'attempted':baseline,'passed':counts.get('passed',0),'failed':counts.get('failed',0),'deferred':counts.get('deferred',0),'remaining':max(0,row[3]-baseline),'complete':bool(row[4])}

    def ensure_read_model(self,conn):
        # Compact projections keep first-load reads away from the million-row
        # validation queue and large metadata/cache payload pages. SQLite
        # triggers keep them atomic even across separate worker processes.
        with self.read_model_lock:
            if conn.execute("SELECT 1 FROM cache WHERE key='home-read-model-v1'").fetchone():return
            conn.executescript("""
                BEGIN IMMEDIATE;
                CREATE TABLE IF NOT EXISTS home_ready(key TEXT PRIMARY KEY,checked REAL NOT NULL);
                CREATE TABLE IF NOT EXISTS home_titles(imdb TEXT PRIMARY KEY,kind TEXT,name TEXT,original TEXT,year INTEGER,genres TEXT,votes INTEGER,rating REAL,runtime INTEGER,released TEXT,official_rating TEXT,critic_rating REAL,generation TEXT);
                CREATE TABLE IF NOT EXISTS home_source_status(key TEXT PRIMARY KEY,state TEXT,checked REAL,expires REAL);
                CREATE TRIGGER IF NOT EXISTS home_ready_insert AFTER INSERT ON validation WHEN NEW.state='verified' BEGIN
                    INSERT OR REPLACE INTO home_ready VALUES(NEW.key,NEW.checked);
                END;
                CREATE TRIGGER IF NOT EXISTS home_ready_update AFTER UPDATE OF state,checked ON validation BEGIN
                    DELETE FROM home_ready WHERE key=OLD.key;
                    INSERT OR REPLACE INTO home_ready SELECT NEW.key,NEW.checked WHERE NEW.state='verified';
                END;
                CREATE TRIGGER IF NOT EXISTS home_ready_delete AFTER DELETE ON validation BEGIN
                    DELETE FROM home_ready WHERE key=OLD.key;
                END;
                CREATE TRIGGER IF NOT EXISTS home_status_insert AFTER INSERT ON cache WHEN NEW.key GLOB 'source-status:*' AND json_valid(NEW.value) BEGIN
                    INSERT OR REPLACE INTO home_source_status VALUES(substr(NEW.key,15),json_extract(NEW.value,'$.state'),COALESCE(json_extract(NEW.value,'$.checked_at'),0),NEW.expires);
                END;
                CREATE TRIGGER IF NOT EXISTS home_status_update AFTER UPDATE OF value,expires ON cache WHEN NEW.key GLOB 'source-status:*' AND json_valid(NEW.value) BEGIN
                    INSERT OR REPLACE INTO home_source_status VALUES(substr(NEW.key,15),json_extract(NEW.value,'$.state'),COALESCE(json_extract(NEW.value,'$.checked_at'),0),NEW.expires);
                END;
                CREATE TRIGGER IF NOT EXISTS home_status_delete AFTER DELETE ON cache WHEN OLD.key GLOB 'source-status:*' BEGIN
                    DELETE FROM home_source_status WHERE key=substr(OLD.key,15);
                END;
                INSERT OR REPLACE INTO home_ready SELECT key,checked FROM validation WHERE state='verified';
                INSERT OR REPLACE INTO home_source_status SELECT substr(key,15),json_extract(value,'$.state'),COALESCE(json_extract(value,'$.checked_at'),0),expires FROM cache WHERE key GLOB 'source-status:*' AND json_valid(value);
                INSERT OR REPLACE INTO cache VALUES('home-read-model-v1','true',4102444800);
                COMMIT;
            """)

    def cache_ready_metadata(self,conn,index,columns):
        identity=index.stat()
        generation=f'{identity.st_dev}:{identity.st_ino}:{identity.st_mtime_ns}:{identity.st_size}'
        missing=conn.execute('SELECT r.imdb FROM ready_titles r LEFT JOIN state.home_titles h ON h.imdb=r.imdb WHERE h.imdb IS NULL OR h.generation!=?',(generation,)).fetchall()
        if missing:
            fields=['imdb','kind','name','original','year','genres','votes','rating','runtime','released','official_rating','critic_rating']
            selected=','.join(field if field in columns else 'NULL' for field in fields)
            fresh=[]
            for start in range(0,len(missing),400):
                batch=[r[0] for r in missing[start:start+400]]
                fresh+=conn.execute('SELECT '+selected+' FROM titles WHERE imdb IN ('+','.join('?' for _ in batch)+')',batch).fetchall()
            if fresh:
                conn.executemany('INSERT OR REPLACE INTO state.home_titles VALUES ('+','.join('?' for _ in range(13))+')',((*row,generation) for row in fresh))
                conn.commit()
        return identity

    def foreground_busy(self):
        try:return float((self.g['LIBRARY']/'.foreground-read-until').read_text())>time.time()
        except (OSError,ValueError):return False

    @property
    def media(self):return Path(self.g.get('MEDIA_ROOT','/media'))

    @property
    def downloads(self):return Path(self.g.get('DOWNLOADS_ROOT','/downloads'))

    def cached(self,key):return self.g['cached'](key)
    def remember(self,key,value,ttl):return self.g['remember'](key,value,ttl)
    def fetch(self,*a,**kw):return self.g['fetch'](*a,**kw)
    def jf(self,*a,**kw):return self.g['jf'](*a,**kw)
    def log(self,text):self.g['log'](text)

    def localize(self,meta):
        result=dict(meta);imdb=meta.get('imdb_id') or meta.get('id')
        kind=meta.get('type')
        with self.db() as conn:
            row=conn.execute('SELECT hebrew,english FROM aliases WHERE imdb=?',(imdb,)).fetchone()
            localized=conn.execute('SELECT title_he,description_he FROM localized_metadata WHERE kind=? AND imdb=?',
                                   (kind,imdb)).fetchone() if kind in ('movie','series') else None
        hebrew=(localized[0] if localized else None) or (row[0] if row else None)
        if hebrew and re.search('[\u0590-\u05ff]',hebrew):
            english=(row[1] if row else None) or meta.get('name','')
            result['englishName']=english
            result['name']=hebrew
            if english and english!=hebrew:result['originalName']=english
        if localized and localized[1] and re.search('[\u0590-\u05ff]',localized[1]):
            result['englishDescription']=meta.get('description') or ''
            result['description']=localized[1]
        return result

    def wikimedia_json(self,url,timeout=8):
        """Serialize Wikimedia reads at one per second and honor provider backoff."""
        lock_path=self.g['DATA']/'wikimedia-api.lock'
        lock_path.parent.mkdir(parents=True,exist_ok=True)
        with lock_path.open('a+') as lock:
            try:fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
            except BlockingIOError:raise RuntimeError('Wikimedia provider busy') from None
            budget_key='wikimedia-request-pacing-v2'
            with self.db() as conn:
                row=conn.execute('SELECT value FROM cache WHERE key=?',(budget_key,)).fetchone()
                budget=json.loads(row[0]) if row else {'requests':0,'last_request':0}
                if budget.get('retry_until',0)>time.time():raise RuntimeError('Wikimedia provider backoff')
                pause=max(0,1-(time.time()-budget.get('last_request',0)))
                if pause:time.sleep(pause)
                budget={'requests':budget.get('requests',0)+1,'last_request':time.time()}
                conn.execute('INSERT OR REPLACE INTO cache VALUES (?,?,?)',
                             (budget_key,json.dumps(budget),time.time()+86400))
            headers={'User-Agent':'HomeMediaCatalogue/1.0 (+https://github.com/<GITHUB_OWNER>/home-media-server)'}
            try:
                result=self.fetch(url,headers=headers,timeout=timeout)
                if isinstance(result,dict) and result.get('error'):
                    self.remember(budget_key,{**budget,'retry_until':time.time()+60},86400)
                    raise RuntimeError('Wikimedia provider temporarily unavailable')
                return result
            except urllib.error.HTTPError as error:
                if error.code in (429,503):
                    try:delay=max(5,min(86400,int(error.headers.get('Retry-After','60'))))
                    except (TypeError,ValueError):delay=60
                    self.remember(budget_key,{**budget,'retry_until':time.time()+delay},86400)
                raise

    @staticmethod
    def _hebrew(value):
        return value.strip() if isinstance(value,str) and re.search('[\u0590-\u05ff]',value) else None

    def wikidata_title_metadata(self,kind,imdb,english_name,include_article=True):
        """Find a Wikidata entity only after exact IMDb-property verification."""
        if kind not in ('movie','series') or not re.fullmatch(r'tt\d{5,12}',imdb):return None
        query={'action':'wbsearchentities','search':english_name,'language':'en','uselang':'en',
               'format':'json','limit':30,'maxlag':5}
        found=self.wikimedia_json('https://www.wikidata.org/w/api.php?'+U.urlencode(query)).get('search',[])
        ids=[row['id'] for row in found if re.fullmatch(r'Q\d+',row.get('id',''))][:30]
        if not ids:return None
        query={'action':'wbgetentities','ids':'|'.join(ids),'props':'claims|labels|descriptions|sitelinks/urls',
               'languages':'he|en','sitefilter':'hewiki','format':'json','maxlag':5}
        entities=self.wikimedia_json('https://www.wikidata.org/w/api.php?'+U.urlencode(query)).get('entities',{})
        for ident in ids:
            entity=entities.get(ident,{})
            imdbs=[claim.get('mainsnak',{}).get('datavalue',{}).get('value')
                   for claim in entity.get('claims',{}).get('P345',[])]
            if imdb not in imdbs:continue
            labels=entity.get('labels',{});descriptions=entity.get('descriptions',{})
            title=self._hebrew(labels.get('he',{}).get('value'))
            description=self._hebrew(descriptions.get('he',{}).get('value'))
            hewiki=entity.get('sitelinks',{}).get('hewiki',{}).get('title')
            if not title:title=self._hebrew(hewiki)
            article_used=False
            if include_article and hewiki:
                try:
                    article=self.wikimedia_json('https://he.wikipedia.org/api/rest_v1/page/summary/'+U.quote(hewiki.replace(' ','_'),safe=''),timeout=6)
                    extract=self._hebrew(article.get('extract'))
                    if extract:description=extract[:4000];article_used=True
                except (urllib.error.HTTPError,urllib.error.URLError,TimeoutError,ValueError):pass
            return {'title_he':title,'description_he':description,'source':'wikidata'+('+hewiki' if article_used else '')}
        return None

    def _save_localized_metadata(self,kind,imdb,value,state='complete',retry_days=180):
        value=value or {};now=time.time();retry=now+retry_days*86400
        with self.db() as conn:
            conn.execute('''INSERT INTO localized_metadata(kind,imdb,title_he,description_he,source,state,checked_at,next_attempt)
                VALUES (?,?,?,?,?,?,?,?) ON CONFLICT(kind,imdb) DO UPDATE SET
                title_he=COALESCE(excluded.title_he,localized_metadata.title_he),
                description_he=COALESCE(excluded.description_he,localized_metadata.description_he),
                source=COALESCE(excluded.source,localized_metadata.source),state=excluded.state,
                checked_at=excluded.checked_at,next_attempt=excluded.next_attempt''',
                (kind,imdb,value.get('title_he'),value.get('description_he'),value.get('source'),state,now,retry))
            if value.get('title_he') or value.get('description_he'):
                old=conn.execute('SELECT hebrew,english FROM aliases WHERE imdb=?',(imdb,)).fetchone()
                hebrew=value.get('title_he') or (old[0] if old else None)
                english=(old[1] if old else None) or value.get('english_name')
                if hebrew or english:
                    conn.execute('''INSERT INTO aliases(imdb,hebrew,english) VALUES (?,?,?)
                        ON CONFLICT(imdb) DO UPDATE SET hebrew=COALESCE(excluded.hebrew,aliases.hebrew),
                        english=COALESCE(aliases.english,excluded.english)''',(imdb,hebrew,english))

    def apply_localized_nfo(self,kind,imdb,meta,title_he,description_he):
        path=self.g['LIBRARY']/('movies' if kind=='movie' else 'shows')/imdb/('movie.nfo' if kind=='movie' else 'tvshow.nfo')
        if not path.is_file():return False
        lock_path=self.g['DATA']/'nfo-metadata.lock'
        with lock_path.open('a+') as lock:
            fcntl.flock(lock,fcntl.LOCK_EX)
            try:tree=ET.parse(path);root=tree.getroot()
            except (OSError,ET.ParseError):return False
            changed=False
            english=meta.get('englishName') or meta.get('originalName') or meta.get('name')
            for tag,value in (('title',title_he),('originaltitle',english),('plot',description_he)):
                if not value:continue
                node=root.find(tag)
                if node is None:node=ET.SubElement(root,tag);changed=True
                if node.text!=value:node.text=str(value);changed=True
            if not changed:return False
            ET.indent(root);temporary=path.with_suffix(path.suffix+'.new')
            tree.write(temporary,encoding='utf-8',xml_declaration=True);temporary.replace(path)
            return True

    def refresh_localized_items(self,identities):
        if not identities:return 0
        try:
            native=self.jf('/Items?Recursive=true&IncludeItemTypes=Movie,Series&Fields=ProviderIds,Path&Limit=5000',timeout=20).get('Items',[])
            ids={(('movie' if item.get('Type')=='Movie' else 'series'),item.get('ProviderIds',{}).get('Imdb')):item.get('Id')
                 for item in native if str(item.get('Path','')).startswith(str(self.g['LIBRARY']))}
            users=self.jf('/Users',timeout=5)
            user=next((u for u in users if u.get('Policy',{}).get('IsAdministrator')),users[0] if users else None)
            if not user:raise RuntimeError('No native metadata user')
            refreshed=0
            for kind,imdb in identities:
                ident=ids.get((kind,imdb))
                if not ident:continue
                if self.hebrew_metadata_busy():break
                with self.db() as conn:
                    localized=conn.execute('SELECT title_he,description_he FROM localized_metadata WHERE kind=? AND imdb=?',(kind,imdb)).fetchone()
                    alias=conn.execute('SELECT english FROM aliases WHERE imdb=?',(imdb,)).fetchone()
                dto=dict(self.jf('/Users/'+user['Id']+'/Items/'+ident,timeout=5))
                dto.pop('UserData',None)
                if localized and localized[0]:dto['Name']=localized[0]
                if localized and localized[1]:dto['Overview']=localized[1]
                if alias and alias[0]:dto['OriginalTitle']=alias[0]
                if dto.get('Tags') is None:dto['Tags']=[]
                self.jf('/Items/'+ident,dto,method='POST',timeout=5)
                with self.db() as conn:conn.execute('UPDATE localized_metadata SET refresh_pending=0 WHERE kind=? AND imdb=?',(kind,imdb))
                refreshed+=1
            return refreshed
        except Exception:
            self.log('Hebrew NFO refresh deferred; local metadata remains safely saved')
            return 0

    def refresh_hebrew_metadata(self,limit=16):
        with (self.g['DATA']/'hebrew-metadata-worker.lock').open('a+') as lock:
            try:fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
            except BlockingIOError:return 0
            return self._refresh_hebrew_metadata(limit)

    def _refresh_hebrew_metadata(self,limit=16):
        """Resumable, ready-only Hebrew metadata enrichment and local NFO refresh."""
        if self.hebrew_metadata_busy():
            self.log('Hebrew metadata backfill deferred during playback or foreground catalogue work');return 0
        with self.db() as conn:
            pending_refresh=[(kind,imdb) for kind,imdb in conn.execute(
                'SELECT kind,imdb FROM localized_metadata WHERE refresh_pending=1 ORDER BY checked_at LIMIT 8')]
        self.refresh_localized_items(pending_refresh)
        ready=self.ready_titles()
        with self.db() as conn:
            rows=conn.execute('SELECT kind,imdb,meta FROM items ORDER BY rowid').fetchall()
            freshness={}
            for key,checked in conn.execute('SELECT key,checked FROM home_ready'):
                identity=tuple(key.split(':')[:2]);freshness[identity]=max(checked,freshness.get(identity,0))
            discovery={(kind,imdb):raw for kind,imdb,raw in conn.execute('SELECT kind,imdb,meta FROM discovery')}
            known={(kind,imdb):(state,next_attempt,title,description,nfo_written,source) for kind,imdb,state,next_attempt,title,description,nfo_written,source in
                   conn.execute('SELECT kind,imdb,state,next_attempt,title_he,description_he,nfo_written,source FROM localized_metadata')}
        item_metadata={(kind,imdb):json.loads(raw) for kind,imdb,raw in rows}
        ordered=[(kind,imdb) for kind,imdb,_ in rows if kind+':'+imdb in ready]
        ordered.extend((kind,imdb) for key in sorted(ready)
                       for kind,imdb in [key.split(':',1)] if (kind,imdb) not in item_metadata)
        index_metadata={};index=self.g['DATA']/'imdb/catalogue.sqlite'
        if index.is_file():
            with closing(sqlite3.connect('file:'+str(index)+'?mode=ro',uri=True)) as conn:
                for kind,imdb in ordered:
                    if (kind,imdb) in item_metadata or (kind,imdb) in discovery:continue
                    row=conn.execute('SELECT name,original,year,genres FROM titles WHERE kind=? AND imdb=?',(kind,imdb)).fetchone()
                    if row:index_metadata[(kind,imdb)]={'id':imdb,'type':kind,'name':row[0],'originalName':row[1],'releaseInfo':str(row[2]),'genres':(row[3] or '').split(',')}
        newest=sorted(ordered,key=lambda identity:(-freshness.get(identity,0),identity))
        oldest=iter(reversed(newest));fair=[];seen=set()
        for start in range(0,len(newest),3):
            group=newest[start:start+3]+[next(oldest,None)]
            for identity in group:
                if identity is not None and identity not in seen:fair.append(identity);seen.add(identity)
        ordered=fair
        candidates=[];missing_index=[];batch_limit=max(1,min(16,int(limit)))
        for kind,imdb in ordered:
            meta=item_metadata.get((kind,imdb))
            if meta is None and (kind,imdb) in discovery:meta=json.loads(discovery[(kind,imdb)])
            if meta is None:meta=index_metadata.get((kind,imdb))
            if meta is None:missing_index.append((kind,imdb));continue
            meta={**meta,'id':imdb,'type':kind}
            state,next_attempt,title_he,description_he,nfo_written,source=known.get((kind,imdb),('',0,None,None,0,None))
            cached_pending=bool((title_he or description_he) and not nfo_written)
            if next_attempt>time.time() and not cached_pending:continue
            candidates.append((kind,imdb,meta,title_he,description_he,nfo_written,source))
            if len(candidates)>=batch_limit:break
        if len(candidates)<batch_limit and missing_index:
            index=self.g['DATA']/'imdb/catalogue.sqlite'
            if index.is_file():
                with closing(sqlite3.connect('file:'+str(index)+'?mode=ro',uri=True)) as conn:
                    for kind,imdb in missing_index:
                        row=conn.execute('SELECT name,original,year,genres FROM titles WHERE kind=? AND imdb=?',(kind,imdb)).fetchone()
                        if not row:continue
                        meta={'id':imdb,'type':kind,'name':row[0],'originalName':row[1],
                              'releaseInfo':str(row[2]),'genres':(row[3] or '').split(',')}
                        state,next_attempt,title_he,description_he,nfo_written,source=known.get((kind,imdb),('',0,None,None,0,None))
                        cached_pending=bool((title_he or description_he) and not nfo_written)
                        if next_attempt>time.time() and not cached_pending:continue
                        candidates.append((kind,imdb,meta,title_he,description_he,nfo_written,source))
                        if len(candidates)>=batch_limit:break
        if not candidates:return 0
        enriched=changed=attempted=0;refresh=[]
        deadline=time.monotonic()+90
        for kind,imdb,meta,cached_title,cached_description,nfo_written,cached_source in candidates:
            if time.monotonic()>deadline or self.hebrew_metadata_busy():break
            attempted+=1
            title=meta.get('name') or meta.get('originalName') or imdb
            value=None
            if cached_title or cached_description:
                value={'title_he':cached_title,'description_he':cached_description,'source':cached_source or 'cached'}
            else:
                try:value=self.wikidata_title_metadata(kind,imdb,title,include_article=True)
                except Exception as error:
                    if isinstance(error,RuntimeError) or isinstance(error,urllib.error.HTTPError) and error.code in (429,503):break
                    retry=1
                    if isinstance(error,urllib.error.HTTPError):
                        try:retry=max(1,min(7,int(error.headers.get('Retry-After','3600'))//86400 or 1))
                        except (TypeError,ValueError):retry=1
                    self._save_localized_metadata(kind,imdb,None,state='retry',retry_days=retry)
                    continue
                if value:value={**value,'english_name':meta.get('name') or meta.get('originalName') or imdb}
                self._save_localized_metadata(kind,imdb,value,state='complete' if value else 'no_match',retry_days=180 if value else 30)
            if not value:continue
            if value.get('title_he') or value.get('description_he'):enriched+=1
            display=self.localize({**meta,'id':imdb,'type':kind})
            if self.hebrew_metadata_busy():
                self.log('Hebrew NFO updates paused after playback started; cached translations will resume later')
                break
            title_he=value.get('title_he') or self._hebrew(display.get('name'))
            try:
                if self.apply_localized_nfo(kind,imdb,display,title_he,value.get('description_he')):
                    changed+=1;refresh.append((kind,imdb))
                    with self.db() as conn:conn.execute('UPDATE localized_metadata SET refresh_pending=1 WHERE kind=? AND imdb=?',(kind,imdb))
            except (OSError,ET.ParseError):
                self._save_localized_metadata(kind,imdb,value,state='retry',retry_days=1)
                continue
            with self.db() as conn:conn.execute('UPDATE localized_metadata SET nfo_written=1 WHERE kind=? AND imdb=?',(kind,imdb))
        self.refresh_localized_items(refresh)
        self.log(f'Hebrew metadata batch complete: {len(candidates)} selected, {attempted} attempted, {enriched} with Hebrew fields, {changed} NFOs updated')
        return enriched

    def hebrew_metadata_busy(self):
        if self.foreground_busy():return True
        try:
            return any(session.get('NowPlayingItem') for session in self.jf('/Sessions',timeout=3))
        except Exception:
            return True

    def poster_url(self,kind,imdb,badge=None):
        if badge is None:badge=self.g['poster_status'](kind,imdb)
        return f"{self.g['BASE']}/poster/{kind}/{imdb}.jpg?style={self.g.get('POSTER_STYLE','3')}&badge={U.quote(badge,safe='')}"

    def series_episode_counts(self,imdbs):
        """Use saved episode metadata only; do not fetch providers while browsing."""
        imdbs=list(dict.fromkeys(i for i in imdbs if re.fullmatch(r'tt\d{5,12}',i)))
        if not imdbs:return {}
        counts={};today=dt.date.today().isoformat()
        def count_document(raw):
            try:videos=json.loads(raw).get('videos')
            except (ValueError,TypeError,AttributeError):return None
            if not isinstance(videos,list):return None
            episodes=set()
            for video in videos:
                if not isinstance(video,dict):continue
                released=video.get('released')
                if not isinstance(released,str) or not re.match(r'^\d{4}-\d{2}-\d{2}',released) or released[:10]>today:continue
                try:season=int(video['season']);episode=int(video['episode'])
                except (KeyError,TypeError,ValueError):continue
                if season>=0 and episode>0:episodes.add((season,episode))
            return len(episodes)
        def count(raw):
            key=(today,hashlib.sha256(raw.encode()).digest())
            with self.episode_count_lock:
                if key not in self.episode_count_cache:
                    if len(self.episode_count_cache)>=512:self.episode_count_cache.clear()
                    self.episode_count_cache[key]=count_document(raw)
                return self.episode_count_cache[key]
        with self.db() as conn:
            # Stream one metadata document at a time to bound memory usage.
            slots=','.join('?' for _ in imdbs)
            for imdb,raw in conn.execute('SELECT imdb,meta FROM items WHERE key IN ('+slots+')', ['series:'+i for i in imdbs]):
                value=count(raw)
                if value is not None:counts[imdb]=value
            for key,raw in conn.execute('SELECT key,value FROM cache WHERE key IN ('+slots+')', ['meta:series:'+i for i in imdbs]):
                value=count(raw)
                if value is not None:counts[key.split(':')[-1]]=value
        return counts

    def decorate_posters(self,metas):
        # Keep provider artwork in storage; expose only the local badged artwork.
        pending={}
        for meta in metas:
            kind=meta.get('type');imdb=meta.get('id','')
            if kind not in ('movie','series') or not re.fullmatch(r'tt\d{5,12}',imdb):continue
            original=dict(meta)
            if not str(original.get('poster','')).startswith('https://'):
                original['poster']=f'https://images.metahub.space/poster/medium/{imdb}/img'
            pending[(kind,imdb)]=json.dumps(original)
        with self.db() as conn:
            # Opening an unchanged page must not take SQLite's writer lock.
            existing={}
            for kind in ('movie','series'):
                ids=[imdb for k,imdb in pending if k==kind]
                for start in range(0,len(ids),400):
                    batch=ids[start:start+400]
                    for imdb,raw in conn.execute('SELECT imdb,meta FROM discovery WHERE kind=? AND imdb IN ('+','.join('?' for _ in batch)+')',(kind,*batch)):
                        existing[(kind,imdb)]=raw
            changed=[(kind,imdb,raw) for (kind,imdb),raw in pending.items() if existing.get((kind,imdb))!=raw]
            if changed:conn.executemany('INSERT INTO discovery VALUES (?,?,?) ON CONFLICT(kind,imdb) DO UPDATE SET meta=excluded.meta',changed)
        badges=self.g['poster_badges'](metas)
        episodes=self.series_episode_counts([m.get('id','') for m in metas if m.get('type')=='series'])
        for meta in metas:
            kind=meta.get('type');imdb=meta.get('id','')
            if kind in ('movie','series') and re.fullmatch(r'tt\d{5,12}',imdb):
                badge=badges[(kind,imdb)]
                meta['poster']=self.poster_url(kind,imdb,badge)
                meta.pop('episodeCount',None)
                if kind=='series' and imdb in episodes:meta['episodeCount']=episodes[imdb]
        return metas

    def enqueue(self,key,priority=0):
        if not re.fullmatch(r'(movie:tt\d{5,12}|series:tt\d{5,12}:\d+:\d+)',key):return
        with self.db() as conn:
            conn.execute('INSERT INTO validation(key,priority) VALUES (?,?) ON CONFLICT(key) DO UPDATE SET priority=MAX(priority,excluded.priority)',(key,priority))

    def enqueue_many(self,keys,priority=0):
        valid={key for key in keys if re.fullmatch(r'(movie:tt\d{5,12}|series:tt\d{5,12}:\d+:\d+)',key)}
        with self.db() as conn:
            conn.executemany('INSERT INTO validation(key,priority) VALUES (?,?) ON CONFLICT(key) DO UPDATE SET priority=MAX(priority,excluded.priority)',((key,priority) for key in valid))

    def consume_rechecks(self):
        """Consume trusted, atomically written plugin events without HTTP secrets."""
        root=self.g['LIBRARY']/'.recheck-requests'
        for path in sorted((root/'pending').glob('*.json'))[:100]:
            if not re.fullmatch(r'[a-f0-9]{32}\.json',path.name):continue
            try:
                if path.is_symlink() or path.stat().st_size>16384:continue
                request=json.loads(path.read_text())
                ident=request['id'];requested=float(request['requested']);scopes=request['keys']
                if ident!=path.stem or not 0<requested<=time.time()+300 or not isinstance(scopes,list) or not 1<=len(scopes)<=100:raise ValueError('Invalid request')
                if not all(isinstance(scope,str) and re.fullmatch(r'(movie:tt\d{5,12}|series:tt\d{5,12}(?::\d{1,3}(?::\d{1,4})?)?)',scope) for scope in scopes):raise ValueError('Invalid scope')
                for scope in scopes:
                    parts=scope.split(':')
                    if len(parts)>=3 and not 0<=int(parts[2])<=300:raise ValueError('Invalid season')
                    if len(parts)==4 and not 1<=int(parts[3])<=2000:raise ValueError('Invalid episode')
                with self.db() as conn:
                    inserted=conn.execute('INSERT OR IGNORE INTO recheck_requests VALUES (?,?,0)',(ident,requested)).rowcount
                    if inserted:
                        keys=set()
                        for scope in scopes:
                            parts=scope.split(':')
                            if parts[0]=='movie' or len(parts)==4:keys.add(scope)
                            else:
                                prefix=scope+':'
                                keys.update(row[0] for row in conn.execute('SELECT key FROM validation WHERE key>=? AND key<?',(prefix,prefix+'\uffff')))
                                if not any(key.startswith(prefix) for key in keys):keys.add(scope+(':1:1' if len(parts)==2 else ':1'))
                        for key in keys:
                            previous=conn.execute('SELECT requested FROM manual_rechecks WHERE key=?',(key,)).fetchone()
                            if previous and previous[0]>=requested:continue
                            conn.execute("INSERT INTO validation(key,state,priority,next_check,reason) VALUES (?,'pending',1000,0,'User requested fresh verification') ON CONFLICT(key) DO UPDATE SET state='pending',priority=MAX(priority,1000),next_check=0,reason='User requested fresh verification'",(key,))
                            conn.execute('DELETE FROM metadata_waits WHERE key=? AND EXISTS(SELECT 1 FROM verification_retry_waits WHERE key=?)',(key,key))
                            conn.execute('DELETE FROM verification_retry_waits WHERE key=?',(key,))
                            conn.execute("INSERT INTO manual_rechecks VALUES (?,?,?,'pending') ON CONFLICT(key) DO UPDATE SET requested=excluded.requested,request_id=excluded.request_id,status='pending'",(key,requested,ident))
                            # Preserve current resolved transport for any existing
                            # viewer. The worker clears it immediately before its
                            # fresh probe, after playback has become idle.
                            conn.execute('DELETE FROM cache WHERE key=?',('source-status:'+key,))
                            for prefix in ('prepared:','verified-prepared:'):
                                conn.execute('DELETE FROM cache WHERE key=? OR (key>=? AND key<?)',(prefix+key,prefix+key+':',prefix+key+':\uffff'))
                        conn.execute('UPDATE recheck_requests SET count=? WHERE id=?',(len(keys),ident))
                done=root/'processed';done.mkdir(parents=True,exist_ok=True)
                try:path.replace(done/path.name)
                except FileNotFoundError:pass # Another process committed it.
                with self.count_lock:self.count_cache.clear()
            except (FileNotFoundError,PermissionError):continue
            except (ValueError,KeyError,TypeError,UnicodeError):
                rejected=root/'processed';rejected.mkdir(parents=True,exist_ok=True)
                try:path.replace(rejected/(path.name+'.rejected'))
                except FileNotFoundError:pass
                self.log('Invalid local recheck request rejected')

    def manual_recheck_state(self,key):
        with self.db() as conn:row=conn.execute('SELECT status FROM manual_rechecks WHERE key=?',(key,)).fetchone()
        return row[0] if row else None

    def validation_result(self,key,ok,proof=None,reason='',attempt_started=None,background=False):
        self.consume_rechecks()
        self.enqueue(key)
        now=time.time()
        with self.db() as conn:
            report=conn.execute('SELECT requested,status FROM manual_rechecks WHERE key=?',(key,)).fetchone()
            if report:
                budget=self.g.get('BUDGET')
                started=attempt_started if attempt_started is not None else getattr(budget,'validation_started',0) or 0
                is_background=background or getattr(budget,'validation_background',False)
                if started and started<report[0]:return False
                if report[1]=='pending' and (not is_background or started<report[0]):return False
                conn.execute('UPDATE manual_rechecks SET status=? WHERE key=?',('passed' if ok else 'failed',key))
            conn.execute('DELETE FROM metadata_waits WHERE key=? AND EXISTS(SELECT 1 FROM verification_retry_waits WHERE key=?)',(key,key))
            conn.execute('DELETE FROM verification_retry_waits WHERE key=?',(key,))
            attempts=conn.execute('SELECT attempts FROM validation WHERE key=?',(key,)).fetchone()[0]
            attempts=0 if ok else attempts+1
            # Preserve successful proof even after failure, but never let it
            # override a newer failed check. Backoff: 6h, 12h, 1d ... max 30d.
            delay=min(30*86400,21600*2**min(max(0,attempts-1),7))
            conn.execute('UPDATE validation SET state=?,attempts=?,next_check=?,checked=?,proof=COALESCE(?,proof),reason=? WHERE key=?',
                ('verified' if ok else 'unavailable',attempts,0 if ok else now+delay,now,json.dumps(proof) if proof else None,reason[:160],key))
            conn.execute('UPDATE audio_recheck SET completed=? WHERE key=?',(now,key))
            if not ok:conn.execute("DELETE FROM cache WHERE key IN (?,?)",('prepared:'+key,'resolved:'+key))
        reliability=self.g.get('REL')
        if reliability:reliability.audit_result(key,ok)
        with self.count_lock:self.count_cache.clear()
        return True

    def source_state(self,key):
        self.consume_rechecks()
        reported=self.manual_recheck_state(key)
        if reported in ('pending','failed'):return 'pending' if reported=='pending' else 'unavailable'
        if self.local_download(key):return 'downloaded'
        with self.db() as conn:row=conn.execute('SELECT state,checked FROM validation WHERE key=?',(key,)).fetchone()
        if row:
            latest=self.cached('source-status:'+key) or {}
            if latest.get('state')=='unavailable' and latest.get('checked_at',0)>row[1]:return 'unavailable'
            return row[0]
        return 'unknown'

    def ready_keys(self):
        self.consume_rechecks()
        with self.db() as conn:
            reported={row[0] for row in conn.execute("SELECT key FROM manual_rechecks WHERE status!='passed'")}
            rows=conn.execute("SELECT v.key FROM home_ready v LEFT JOIN home_source_status c ON c.key=v.key AND c.expires>? WHERE c.key IS NULL OR c.state!='unavailable' OR c.checked<=v.checked",(time.time(),)).fetchall()
            downloads=conn.execute('SELECT key FROM downloads').fetchall()
        ready={key for (key,) in rows}
        for (key,) in downloads:
            if self.local_download(key):ready.add(key)
        return ready-reported

    def ready_titles(self):
        return {':'.join(key.split(':')[:2]) for key in self.ready_keys()}

    def schedule_audio_resync(self):
        # One migration only. Keep verified titles visible until their audit
        # actually fails; retain negative metadata, attempt counts and proofs.
        marker='audio-policy-resync-v2'
        with self.db() as conn:
            if conn.execute('SELECT 1 FROM cache WHERE key=?',(marker,)).fetchone():return
            now=time.time()
            failed=conn.execute("SELECT COUNT(*) FROM validation WHERE state='unavailable'").fetchone()[0]
            passed=conn.execute("SELECT COUNT(*) FROM validation WHERE state='verified' AND COALESCE(json_extract(proof,'$.audio_policy'),0)<2").fetchone()[0]
            conn.execute("UPDATE validation SET next_check=MIN(next_check,?) WHERE state='unavailable'",(now,))
            conn.execute("INSERT INTO audio_recheck(key) SELECT key FROM validation WHERE state='verified' AND COALESCE(json_extract(proof,'$.audio_policy'),0)<2 ON CONFLICT(key) DO UPDATE SET completed=0")
            conn.execute('DELETE FROM cache WHERE key LIKE ?',('prepared:%',))
            conn.execute('INSERT OR REPLACE INTO cache VALUES (?,?,?)',(marker,json.dumps({'scheduled_at':now,'failed_scheduled':failed,'verified_audits':passed}),now+10*365*86400))

    def seed_validation(self):
        # Keep the queue separate from the replaceable bulk metadata index.
        # Existing entries retain proof, retry schedule and failure count.
        index=self.g['DATA']/'imdb/catalogue.sqlite'
        signature=('validation-seeded:'+str(index.stat().st_ino)) if index.is_file() else None
        seed_bulk=signature is not None and self.cached(signature) is None
        with self.db() as conn:
            if seed_bulk:
                conn.execute('ATTACH DATABASE ? AS bulk',(str(index),))
                conn.execute("INSERT OR IGNORE INTO validation(key,priority) SELECT kind||':'||imdb||CASE WHEN kind='series' THEN ':1:1' ELSE '' END, CASE WHEN votes>10000 THEN 10 ELSE 0 END FROM bulk.titles WHERE year<=? ORDER BY year DESC,votes DESC",(dt.date.today().year,))
                conn.execute('INSERT OR REPLACE INTO cache VALUES (?,?,?)',(signature,'true',time.time()+10*365*86400))
            for kind,imdb in conn.execute('SELECT kind,imdb FROM discovery UNION SELECT kind,imdb FROM items').fetchall():
                key=kind+':'+imdb+(':1:1' if kind=='series' else '')
                conn.execute('INSERT OR IGNORE INTO validation(key,priority) VALUES (?,20)',(key,))
            if not conn.execute("SELECT 1 FROM cache WHERE key='validation-migrated-v2'").fetchone():
                for cachekey,raw in conn.execute("SELECT key,value FROM cache WHERE key LIKE 'source-status:%'").fetchall():
                    status=json.loads(raw);key=cachekey.removeprefix('source-status:')
                    if status.get('state')!='unavailable':continue
                    checked=status.get('checked_at',time.time())
                    conn.execute("UPDATE validation SET state='unavailable',attempts=1,checked=?,next_check=?,reason='Previous unavailable source retained' WHERE key=? AND checked=0",(checked,max(time.time(),checked+21600),key))
                for (key,) in conn.execute('SELECT key FROM playback').fetchall():
                    conn.execute('UPDATE validation SET priority=MAX(priority,150) WHERE key=?',(key,))
                conn.execute('INSERT OR REPLACE INTO cache VALUES (?,?,?)',('validation-migrated-v2','true',time.time()+10*365*86400))


    def page(self,kind,year,skip):
        if kind not in ('movie','series') or not 1920<=year<=dt.date.today().year:raise ValueError('Invalid catalogue year')
        cachekey=f'discover-page:{kind}:{year}:{skip}'
        saved=self.cached(cachekey)
        if saved is not None:return saved
        raw=self.fetch(f"{self.g['CINEMETA']}/catalog/{kind}/year/genre={year}&skip={skip}.json",timeout=12).get('metas',[])
        entries=[]
        with self.db() as conn:
            for entry in raw:
                imdb=entry.get('imdb_id') or entry.get('id','')
                if not re.fullmatch(r'tt\d{5,12}',imdb):continue
                if (entry.get('released') or '')[:10]>dt.date.today().isoformat():continue
                item=dict(entry);item['id']=imdb
                conn.execute('INSERT OR REPLACE INTO discovery VALUES (?,?,?)',(kind,imdb,json.dumps(item)))
                entries.append(item)
                key=kind+':'+imdb+(':1:1' if kind=='series' else '')
                conn.execute('INSERT INTO validation(key,priority) VALUES (?,40) ON CONFLICT(key) DO UPDATE SET priority=MAX(priority,40)',(key,))
        self.record_dates(kind,entries)
        return self.remember(cachekey,{'metas':entries,'raw_count':len(raw)},21600)

    @staticmethod
    def parse_years(value):
        if value is None or value=='':return []
        if isinstance(value,(tuple,list,set)):parts=[str(v) for v in value]
        else:parts=str(value).split(',')
        years=set()
        for part in parts:
            match=re.fullmatch(r'\s*(\d{4})(?:\s*[-–—]\s*(\d{4}))?\s*',part)
            if not match:raise ValueError('Use a year such as 2025 or a range such as 1994-2000')
            start=int(match[1]);end=int(match[2] or match[1])
            if not 1920<=start<=end<=dt.date.today().year:raise ValueError('Invalid year range')
            years.update(range(start,end+1))
        return sorted(years,reverse=True)

    @staticmethod
    def parse_months(value):
        match=re.fullmatch(r'\s*(\d{4})-(\d{2})(?:\s*\.\.\s*(\d{4})-(\d{2}))?\s*',str(value))
        if not match:raise ValueError('Use YYYY-MM or YYYY-MM..YYYY-MM')
        start=dt.date(int(match[1]),int(match[2]),1)
        end=dt.date(int(match[3] or match[1]),int(match[4] or match[2]),1)
        if not 1920<=start.year<=end.year<=dt.date.today().year or end<start:
            raise ValueError('Invalid release month range')
        after=dt.date(end.year+1,1,1) if end.month==12 else dt.date(end.year,end.month+1,1)
        return start.isoformat(),after.isoformat()

    def record_dates(self,kind,metas,apply=True):
        dates=[]
        for meta in metas:
            imdb=meta.get('id') or meta.get('imdb_id','')
            value=str(meta.get('released') or '')
            if kind not in ('movie','series') or not re.fullmatch(r'tt\d{5,12}',imdb):continue
            if not re.match(r'^\d{4}-\d{2}-\d{2}(?:T|$)',value):continue
            try:released=dt.date.fromisoformat(value[:10])
            except ValueError:continue
            if not 1920<=released.year<=dt.date.today().year+5:continue
            dates.append((kind,imdb,released.isoformat()))
        if not dates:return
        with self.db() as conn:
            before=conn.total_changes
            conn.executemany('INSERT INTO release_dates VALUES (?,?,?) ON CONFLICT(kind,imdb) DO UPDATE SET released=excluded.released WHERE release_dates.released!=excluded.released',dates)
            changed=conn.total_changes>before
        if apply and changed:self.apply_dates(dates=dates)

    def apply_dates(self,target=None,dates=None):
        target=target or self.g['DATA']/'imdb/catalogue.sqlite'
        if not target.is_file():return
        if dates is None:
            with self.db() as conn:dates=conn.execute('SELECT kind,imdb,released FROM release_dates').fetchall()
        with closing(sqlite3.connect(target,timeout=30)) as conn:
            if 'released' not in {r[1] for r in conn.execute('PRAGMA table_info(titles)')}:return
            conn.executemany("UPDATE titles SET released=? WHERE kind=? AND imdb=? AND COALESCE(released,'')!=?",
                [(released,kind,imdb,released) for kind,imdb,released in dates])
            conn.commit()

    def backfill_dates(self):
        with self.db() as conn:
            rows=conn.execute("SELECT kind,imdb,json_extract(meta,'$.released') FROM items UNION SELECT kind,imdb,json_extract(meta,'$.released') FROM discovery").fetchall()
            rows += [(key.split(':')[1],key.split(':')[2],released) for key,released in conn.execute("SELECT key,json_extract(value,'$.released') FROM cache WHERE key LIKE 'meta:movie:%' OR key LIKE 'meta:series:%'")]
        for kind in ('movie','series'):
            self.record_dates(kind,[{'id':imdb,'released':released} for typ,imdb,released in rows if typ==kind],apply=False)
        self.apply_dates()
        with self.db() as conn:count=conn.execute('SELECT COUNT(*) FROM release_dates').fetchone()[0]
        self.log(f'Real release dates restored from existing metadata: {count}')

    def sync_release_dates(self,years=None,max_pages=100,force=False):
        for kind in ('movie','series'):
            for year in years or (dt.date.today().year,dt.date.today().year-1):
                cachekey=f'release-pages:{kind}:{year}'
                if not force and self.cached(cachekey):continue
                seen=set();skip=0;dated=0;complete=False
                for _ in range(max_pages):
                    try:
                        metas=self.fetch(f"{self.g['CINEMETA']}/catalog/{kind}/year/genre={year}&skip={skip}.json",timeout=8).get('metas',[])
                        ids={m.get('id') for m in metas}
                        if not metas or not ids-seen:complete=True;break
                        self.record_dates(kind,metas,apply=False)
                        seen.update(ids);skip+=len(metas)
                        dated+=sum(bool(m.get('released')) for m in metas)
                        time.sleep(.15)
                    except Exception:
                        self.log(f'Release month enrichment deferred: {kind} {year}');break
                self.apply_dates()
                if complete:self.remember(cachekey,True,5*3600)
                self.log(f'Release month enrichment: {kind} {year}, {dated} provider dates')

    def browse(self,kind,offset=0,limit=100,year=None,sort_by='PremiereDate',sort_order='Descending',month=None):
        offset=max(0,int(offset));limit=max(1,min(1000,int(limit)))
        years_filter=self.parse_years(year)
        month_bounds=self.parse_months(month) if month else None
        if kind not in ('movie','series','all'):
            raise ValueError('Invalid catalogue request')
        index=self.g['DATA']/'imdb/catalogue.sqlite'
        if index.is_file():
            self.db().close()
            with closing(sqlite3.connect('file:'+str(index)+'?mode=ro',uri=True)) as conn:
                conn.execute('ATTACH DATABASE ? AS state',(str(self.g['DATA']/'catalogue.sqlite'),))
                columns={r[1] for r in conn.execute('PRAGMA table_info(titles)')}
                where="t.kind IN ('movie','series')" if kind=='all' else 't.kind=?';params=[] if kind=='all' else [kind]
                if years_filter:
                    where+=' AND t.year IN ('+','.join('?' for _ in years_filter)+')';params.extend(years_filter)
                if month_bounds:
                    if 'released' not in columns:return {'metas':[],'total':0,'has_more':False,'offset':offset}
                    where+=' AND t.released>=? AND t.released<?';params.extend(month_bounds)
                if not years_filter or dt.date.today().year in years_filter:
                    where+=' AND (t.year<? OR t.votes>0 OR t.imdb IN (SELECT imdb FROM state.release_dates WHERE released<=?))'
                    params.extend([dt.date.today().year,dt.date.today().isoformat()])
                ready=sorted(self.ready_titles())
                # Temp table avoids SQLite variable limits as the library grows.
                conn.execute('CREATE TEMP TABLE ready_titles(kind TEXT,imdb TEXT,PRIMARY KEY(kind,imdb))')
                conn.executemany('INSERT INTO ready_titles VALUES (?,?)',(v.split(':',1) for v in ready))
                where+=' AND t.imdb=ready.imdb AND t.kind=ready.kind'
                unavailable=[row[0] for row in conn.execute("SELECT kind||':'||imdb FROM state.release_dates WHERE released>?",(dt.date.today().isoformat(),))]
                if unavailable:
                    where+=" AND (t.kind||':'||t.imdb) NOT IN ("+','.join('?' for _ in unavailable)+')'
                    params.extend(unavailable)
                # Iterate the small verified set, then seek each IMDb primary
                # key. Scanning a million ordered rows is costly on this HDD.
                identity=self.cache_ready_metadata(conn,index,columns)
                count_source='ready_titles ready CROSS JOIN state.home_titles t INDEXED BY sqlite_autoindex_home_titles_1'
                count_key=(kind,tuple(years_filter),month_bounds,identity.st_ino,identity.st_mtime_ns,
                    hashlib.sha256(json.dumps((ready,sorted(unavailable))).encode()).digest())
                with self.count_lock:
                    entry=self.count_cache.get(count_key)
                    if entry and entry[0]>time.monotonic():total=entry[1]
                    else:
                        total=conn.execute('SELECT COUNT(*) FROM '+count_source+' WHERE '+where,params).fetchone()[0]
                        if len(self.count_cache)>=64:self.count_cache.clear()
                        self.count_cache[count_key]=(time.monotonic()+60,total)
                sorts={'votes':'t.votes','popularity':'t.votes','sortname':'t.name COLLATE NOCASE','name':'t.name COLLATE NOCASE',
                    'communityrating':'t.rating','rating':'t.rating','runtime':'t.runtime','premieredate':'t.released',
                    'datecreated':'t.released','officialrating':'t.official_rating','criticrating':'t.critic_rating'}
                expression=sorts.get(str(sort_by).lower(),'t.votes')
                field=expression.split('.')[-1].split()[0]
                if field not in columns:expression='NULL'
                order='ASC' if str(sort_order).lower() in ('ascending','asc') else 'DESC'
                rank_source=count_source
                ordering=f't.year DESC,{expression} {order} NULLS LAST,t.imdb'
                # Rank compact index entries first; fetch full rows/aliases only
                # for this page, rather than sorting thousands of poster previews.
                rows=conn.execute('WITH ranked AS (SELECT t.imdb FROM '+rank_source+' WHERE '+where+
                    ' ORDER BY '+ordering+' LIMIT ? OFFSET ?) SELECT t.imdb,t.name,t.original,t.year,t.genres,'+
                    'COALESCE(lm.title_he,a.hebrew),lm.description_he,COALESCE(a.english,t.name),'+
                    ('t.rating' if 'rating' in columns else 'NULL')+','+('t.runtime' if 'runtime' in columns else 'NULL')+
                    ',t.kind,'+('t.released' if 'released' in columns else 'NULL')+
                    ' FROM ranked r JOIN state.home_titles t ON t.imdb=r.imdb LEFT JOIN state.aliases a ON a.imdb=t.imdb '
                    'LEFT JOIN state.localized_metadata lm ON lm.imdb=t.imdb AND lm.kind=t.kind ORDER BY '+ordering,
                    (*params,limit,offset)).fetchall()
            metas=[{'id':imdb,'type':actual_kind,'name':hebrew or name,'englishName':english,'originalName':english,
                'description':description_he,
                'releaseInfo':str(release_year),'genres':genres.split(',') if genres else [],
                'poster':f'https://images.metahub.space/poster/medium/{imdb}/img',
                'released':released+'T00:00:00Z' if released else None,
                'imdbRating':str(rating) if rating is not None else None,'runtime':str(runtime)+' min' if runtime else None}
                for imdb,name,original,release_year,genres,hebrew,description_he,english,rating,runtime,actual_kind,released in rows]
            return {'metas':self.decorate_posters(metas),'total':total,'has_more':offset+len(metas)<total,'offset':offset}
        if month_bounds:raise RuntimeError('Month search requires the local release date index')
        cachekey=f'browse:{kind}:{years_filter}:{offset}:{limit}:{sort_by}:{sort_order}'
        saved=self.cached(cachekey)
        if saved is not None:return saved
        result=[];seen=set();position=0;has_more=False
        years=years_filter or range(dt.date.today().year,1919,-1)
        for current in years:
            skip=0
            while True:
                page=self.page(kind,current,skip)
                for entry in page['metas']:
                    imdb=entry['id']
                    if imdb in seen:continue
                    seen.add(imdb)
                    if kind+':'+imdb not in self.ready_titles():continue
                    if position>=offset and len(result)<limit:result.append(self.localize(entry))
                    position+=1
                    if len(result)==limit:
                        has_more=True
                        break
                if len(result)==limit:break
                count=page['raw_count']
                if count==0:break
                skip+=count
                # Provider page sizes can change; skip by the number returned.
            if len(result)==limit:break
        return self.remember(cachekey,{'metas':result,'has_more':has_more,
            'total':offset+len(result)+(limit if has_more else 0),'offset':offset},1800)

    def update_index(self):
        folder=self.g['DATA']/'imdb';folder.mkdir(parents=True,exist_ok=True)
        target=folder/'catalogue.sqlite'
        marker=folder/'last-full-sync'
        baseline=marker.stat().st_mtime if marker.exists() else (folder/'title.basics.tsv.gz').stat().st_mtime if (folder/'title.basics.tsv.gz').exists() else 0
        if target.is_file() and time.time()-baseline<20*3600:
            self.ensure_search_index(target)
            self.ensure_sort_index(target)
            self.apply_dates(target)
            self.log('Daily IMDb catalogue index already current');return
        for name in ('basics','ratings'):
            path=folder/f'title.{name}.tsv.gz'
            if path.exists() and time.time()-path.stat().st_mtime<20*3600:continue
            url=f'https://datasets.imdbws.com/title.{name}.tsv.gz'
            request=R.Request(url,headers={'User-Agent':'HomeMediaCatalogue/1.0 (personal non-commercial use)'})
            with R.urlopen(request,timeout=60) as response, path.with_suffix('.download').open('wb') as output:
                shutil.copyfileobj(response,output,length=1024*1024)
            path.with_suffix('.download').replace(path)
        staging=folder/'catalogue.building.sqlite'
        staging.unlink(missing_ok=True)
        conn=sqlite3.connect(staging)
        try:
            conn.execute('PRAGMA journal_mode=OFF');conn.execute('PRAGMA synchronous=OFF')
            conn.execute('PRAGMA cache_size=-16384')
            conn.execute('CREATE TABLE titles (imdb TEXT PRIMARY KEY,kind TEXT,name TEXT,original TEXT,year INTEGER,genres TEXT,votes INTEGER DEFAULT 0)')
            batch=[];count=0
            with gzip.open(folder/'title.basics.tsv.gz','rt',encoding='utf-8',newline='') as source:
                for row in csv.DictReader(source,delimiter='\t',quoting=csv.QUOTE_NONE):
                    typ=row['titleType'];year=row['startYear']
                    if typ not in ('movie','tvMovie','tvSeries','tvMiniSeries') or row['isAdult']!='0' or not year.isdigit():continue
                    if not 1920<=int(year)<=dt.date.today().year:continue
                    batch.append((row['tconst'],'movie' if typ in ('movie','tvMovie') else 'series',
                                  row['primaryTitle'],row['originalTitle'],int(year),row['genres']))
                    if len(batch)>=2000:
                        conn.executemany('INSERT OR REPLACE INTO titles(imdb,kind,name,original,year,genres) VALUES (?,?,?,?,?,?)',batch)
                        count+=len(batch);batch=[]
                if batch:
                    conn.executemany('INSERT OR REPLACE INTO titles(imdb,kind,name,original,year,genres) VALUES (?,?,?,?,?,?)',batch);count+=len(batch)
            conn.commit();self.log(f'IMDb title metadata indexed: {count}')
            batch=[]
            with gzip.open(folder/'title.ratings.tsv.gz','rt',encoding='utf-8',newline='') as source:
                for row in csv.DictReader(source,delimiter='\t',quoting=csv.QUOTE_NONE):
                    batch.append((int(row['numVotes']),row['tconst']))
                    if len(batch)>=2000:
                        conn.executemany('UPDATE titles SET votes=? WHERE imdb=?',batch);batch=[]
                if batch:conn.executemany('UPDATE titles SET votes=? WHERE imdb=?',batch)
            conn.execute('CREATE INDEX browse_titles ON titles(kind,year DESC,votes DESC,imdb)')
            conn.execute("CREATE VIRTUAL TABLE title_fts USING fts5(name,original,content='titles',content_rowid='rowid')")
            conn.execute("INSERT INTO title_fts(title_fts) VALUES ('rebuild')")
            conn.commit();conn.execute('PRAGMA optimize')
            counts=conn.execute('SELECT kind,COUNT(*) FROM titles GROUP BY kind').fetchall()
        finally:conn.close()
        staging.replace(target)
        self.ensure_sort_index(target)
        self.apply_dates(target)
        marker.write_text(dt.datetime.now(dt.timezone.utc).isoformat()+'\n')
        self.log('Full catalogue index ready: '+str(dict(counts)))

    def ensure_sort_index(self,target):
        with closing(sqlite3.connect('file:'+str(target)+'?mode=ro',uri=True)) as conn:
            if {'rating','runtime','released','added_at','official_rating','critic_rating'}.issubset({r[1] for r in conn.execute('PRAGMA table_info(titles)')}):return self.ensure_browse_indexes(target)
        staging=target.with_name('catalogue.sort-building.sqlite');shutil.copyfile(target,staging)
        with closing(sqlite3.connect(staging)) as conn:
            conn.execute('PRAGMA journal_mode=OFF');conn.execute('PRAGMA cache_size=-16384')
            existing={r[1] for r in conn.execute('PRAGMA table_info(titles)')}
            for name,typ in [('rating','REAL'),('runtime','INTEGER'),('released','TEXT'),('added_at','REAL'),('official_rating','TEXT'),('critic_rating','REAL')]:
                if name not in existing:conn.execute(f'ALTER TABLE titles ADD COLUMN {name} {typ}')
            conn.execute('UPDATE titles SET added_at=? WHERE added_at IS NULL',(time.time(),))
            ratings=target.parent/'title.ratings.tsv.gz';basics=target.parent/'title.basics.tsv.gz'
            if ratings.exists():
                batch=[]
                with gzip.open(ratings,'rt',encoding='utf-8',newline='') as source:
                    for row in csv.DictReader(source,delimiter='\t',quoting=csv.QUOTE_NONE):
                        batch.append((float(row['averageRating']),row['tconst']))
                        if len(batch)>=4000:conn.executemany('UPDATE titles SET rating=? WHERE imdb=?',batch);batch=[]
                    if batch:conn.executemany('UPDATE titles SET rating=? WHERE imdb=?',batch)
            if basics.exists():
                batch=[]
                with gzip.open(basics,'rt',encoding='utf-8',newline='') as source:
                    for row in csv.DictReader(source,delimiter='\t',quoting=csv.QUOTE_NONE):
                        if row['titleType'] not in ('movie','tvMovie','tvSeries','tvMiniSeries') or not row['runtimeMinutes'].isdigit():continue
                        batch.append((int(row['runtimeMinutes']),row['tconst']))
                        if len(batch)>=4000:conn.executemany('UPDATE titles SET runtime=? WHERE imdb=?',batch);batch=[]
                    if batch:conn.executemany('UPDATE titles SET runtime=? WHERE imdb=?',batch)
            conn.execute('CREATE INDEX IF NOT EXISTS browse_score ON titles(kind,year DESC,rating DESC,imdb)')
            conn.execute('CREATE INDEX IF NOT EXISTS browse_name ON titles(kind,year DESC,name COLLATE NOCASE,imdb)')
            conn.commit()
        staging.replace(target)
        self.ensure_browse_indexes(target)
        self.log('IMDb score and runtime sorting data ready')

    def ensure_search_index(self,target):
        with closing(sqlite3.connect('file:'+str(target)+'?mode=ro',uri=True)) as conn:
            if conn.execute("SELECT 1 FROM sqlite_master WHERE name='title_fts'").fetchone():return
        staging=target.with_name('catalogue.search-building.sqlite')
        shutil.copyfile(target,staging)
        with closing(sqlite3.connect(staging)) as conn:
            conn.execute('PRAGMA journal_mode=OFF');conn.execute('PRAGMA cache_size=-16384')
            conn.execute("CREATE VIRTUAL TABLE title_fts USING fts5(name,original,content='titles',content_rowid='rowid')")
            conn.execute("INSERT INTO title_fts(title_fts) VALUES ('rebuild')");conn.commit()
        staging.replace(target)
        self.log('Full-text title search index ready')

    def ensure_browse_indexes(self,target):
        expected={'browse_name_cover','browse_score_cover','browse_runtime_cover','browse_release_cover','browse_release_lookup'}
        with closing(sqlite3.connect('file:'+str(target)+'?mode=ro',uri=True)) as conn:
            if expected.issubset({row[1] for row in conn.execute('PRAGMA index_list(titles)')}):return
        staging=target.with_name('catalogue.covering-building.sqlite')
        shutil.copyfile(target,staging)
        with closing(sqlite3.connect(staging)) as conn:
            conn.execute('PRAGMA journal_mode=OFF');conn.execute('PRAGMA cache_size=-16384')
            for name,expression in [('name','name COLLATE NOCASE'),('score','rating DESC'),('runtime','runtime DESC')]:
                conn.execute(f'CREATE INDEX IF NOT EXISTS browse_{name}_cover ON titles(kind,year DESC,{expression},imdb,votes)')
            conn.execute('CREATE INDEX IF NOT EXISTS browse_release_cover ON titles(kind,year DESC,released DESC,imdb,votes)')
            conn.execute('CREATE INDEX IF NOT EXISTS browse_release_lookup ON titles(kind,released,year,imdb,votes) WHERE released IS NOT NULL')
            conn.commit();conn.execute('PRAGMA optimize')
        staging.replace(target)
        self.log('Covering catalogue sort indexes ready; reader files replaced atomically')

    def hebrew_search(self,kind,query):
        key='hebrew-search-v2:'+kind+':'+query.strip()
        saved=self.cached(key)
        if saved is not None:return saved
        params={'action':'wbsearchentities','search':query,'language':'he','uselang':'he',
                'format':'json','limit':20,'maxlag':30}
        response=self.wikimedia_json('https://www.wikidata.org/w/api.php?'+U.urlencode(params),timeout=7)
        if response.get('error'):raise RuntimeError('Hebrew title provider temporarily busy')
        found=response.get('search',[])
        ids=[r['id'] for r in found if re.fullmatch(r'Q\d+',r.get('id',''))]
        if not ids:return self.remember(key,[],3600)
        params={'action':'wbgetentities','ids':'|'.join(ids),'props':'claims|labels',
                'languages':'he|en','format':'json','maxlag':30}
        response=self.wikimedia_json('https://www.wikidata.org/w/api.php?'+U.urlencode(params),timeout=7)
        if response.get('error'):raise RuntimeError('Hebrew title provider temporarily busy')
        entities=response.get('entities',{})
        output=[];keywords=[];lookups=0
        for ident in ids:
            entity=entities.get(ident,{})
            imdbs=[c.get('mainsnak',{}).get('datavalue',{}).get('value') for c in entity.get('claims',{}).get('P345',[])]
            labels=entity.get('labels',{});hebrew=labels.get('he',{}).get('value');english=labels.get('en',{}).get('value')
            if english:
                root=re.sub(r'\s*\([^)]*\)\s*$','',english).split(':')[0].strip()
                if root and re.search('[A-Za-z]',root) and root.lower() not in {k.lower() for k in keywords}:keywords.append(root)
            for imdb in imdbs:
                if not isinstance(imdb,str) or not re.fullmatch(r'tt\d{5,12}',imdb):continue
                try:
                    meta=None
                    index=self.g['DATA']/'imdb/catalogue.sqlite'
                    if index.exists():
                        with closing(sqlite3.connect('file:'+str(index)+'?mode=ro',uri=True)) as conn:
                            row=conn.execute('SELECT name,original,year,genres FROM titles WHERE imdb=? AND kind=?',(imdb,kind)).fetchone()
                        if row:
                            meta={'id':imdb,'type':kind,'name':row[0],'originalName':row[1],
                                'releaseInfo':str(row[2]),'genres':row[3].split(','),
                                'poster':f'https://images.metahub.space/poster/medium/{imdb}/img'}
                        else:continue # A known series must never be returned as a movie.
                    if meta is None:
                        if lookups>=3 and self.cached('meta:'+kind+':'+imdb) is None:continue
                        lookups+=1
                        meta=self.g['metadata'](kind,imdb)
                    if not isinstance(meta,dict) or meta.get('type') not in (None,kind):continue
                    with self.db() as conn:
                        conn.execute('INSERT OR REPLACE INTO aliases VALUES (?,?,?)',(imdb,hebrew,english or meta.get('name')))
                    output.append(self.localize(meta))
                except (ValueError,urllib.error.HTTPError,KeyError):continue
        self.remember('hebrew-keywords:'+self.search_key(query),keywords[:5],7*86400)
        return self.remember(key,output,86400)

    @staticmethod
    def search_key(query):
        import unicodedata
        value=''.join(c for c in unicodedata.normalize('NFKD',query) if not unicodedata.combining(c))
        return re.sub(r'[\s\-־–—\"\u05f3\u05f4\x27]','',value).casefold()

    def index_search(self,kind,query):
        path=self.g['DATA']/'imdb/catalogue.sqlite'
        if not path.exists():return []
        term='%'+query.replace('\\','\\\\').replace('%','\\%').replace('_','\\_')+'%'
        self.db().close()
        with closing(sqlite3.connect('file:'+str(path)+'?mode=ro',uri=True)) as conn:
            conn.execute('ATTACH DATABASE ? AS state',(str(self.g['DATA']/'catalogue.sqlite'),))
            aliases=[r[0] for r in conn.execute("SELECT imdb FROM state.aliases WHERE hebrew LIKE ? ESCAPE '\\' LIMIT 100",(term,))]
            rows=[]
            select='SELECT t.imdb,t.name,t.original,t.year,t.genres,a.hebrew FROM titles t LEFT JOIN state.aliases a ON a.imdb=t.imdb '
            if aliases:
                rows=conn.execute(select+'WHERE t.kind=? AND t.imdb IN ('+','.join('?' for _ in aliases)+')',(kind,*aliases)).fetchall()
            tokens=re.findall(r'[^\W_]+',query,re.UNICODE)
            fts=conn.execute("SELECT 1 FROM sqlite_master WHERE name='title_fts'").fetchone()
            if tokens and fts:
                expression=' AND '.join('"'+t+'"*' for t in tokens[:8])
                rows+=conn.execute(select+"WHERE t.kind=? AND t.rowid IN (SELECT rowid FROM title_fts WHERE title_fts MATCH ? LIMIT 2000) "
                    "AND (t.year<? OR t.votes>0) ORDER BY t.votes DESC,t.year DESC LIMIT 100",(kind,expression,dt.date.today().year)).fetchall()
            elif not aliases:
                rows=conn.execute(select+"WHERE t.kind=? AND (t.name LIKE ? ESCAPE '\\' OR t.original LIKE ? ESCAPE '\\') "
                    "AND (t.year<? OR t.votes>0) ORDER BY t.votes DESC LIMIT 100",(kind,term,term,dt.date.today().year)).fetchall()
        return [{'id':imdb,'type':kind,'name':he+' / '+name if he else name,'originalName':original,
                 'releaseInfo':str(year),'genres':genres.split(','),
                 'poster':f'https://images.metahub.space/poster/medium/{imdb}/img'}
                for imdb,name,original,year,genres,he in rows]

    @staticmethod
    def search_words(value):
        import unicodedata
        value=''.join(c for c in unicodedata.normalize('NFKD',str(value or '')) if not unicodedata.combining(c))
        return re.findall(r'[^\W_]+',value.casefold(),re.UNICODE)

    def search_rank(self,meta,query):
        words=self.search_words(query)
        years=[w for w in words if re.fullmatch(r'(19|20)\d{2}',w)]
        words=[w for w in words if w not in years]
        scores=[]
        for name in (meta.get('name'),meta.get('originalName')):
            title=self.search_words(name);phrase=' '.join(title);wanted=' '.join(words)
            matches=sum(any(t.startswith(w) for t in title) for w in words)
            scores.append((1000 if wanted and phrase==wanted else 600 if wanted and phrase.startswith(wanted) else 300 if words and matches==len(words) else 0)+matches*10)
        year=str(meta.get('releaseInfo',''))[:4]
        return max(scores,default=0)+(100 if year in years else 0)

    def ready_search(self,kind,query):
        """Search the small ready identity set before bulk/provider truncation."""
        ready=sorted(k.split(':')[1] for k in self.ready_titles() if k.startswith(kind+':'))
        if not ready:return []
        values={};aliases={}
        with self.db() as c:
            for start in range(0,len(ready),400):
                ids=ready[start:start+400];marks=','.join('?' for _ in ids)
                for imdb,raw in c.execute('SELECT imdb,meta FROM items WHERE kind=? AND imdb IN ('+marks+')',(kind,*ids)):
                    values[imdb]=json.loads(raw)
                aliases.update((imdb,(he,en)) for imdb,he,en in c.execute('SELECT imdb,hebrew,english FROM aliases WHERE imdb IN ('+marks+')',ids))
        index=self.g['DATA']/'imdb/catalogue.sqlite'
        if index.is_file():
            with closing(sqlite3.connect('file:'+str(index)+'?mode=ro',uri=True)) as c:
                for start in range(0,len(ready),400):
                    ids=ready[start:start+400];marks=','.join('?' for _ in ids)
                    for imdb,name,original,year,genres in c.execute('SELECT imdb,name,original,year,genres FROM titles WHERE kind=? AND imdb IN ('+marks+')',(kind,*ids)):
                        values[imdb]={**values.get(imdb,{}),'id':imdb,'type':kind,'name':name,'originalName':original,'releaseInfo':str(year),'genres':(genres or '').split(',')}
        words=self.search_words(query);years={w for w in words if re.fullmatch(r'(19|20)\d{2}',w)}
        words=[w for w in words if w not in years];output=[]
        for imdb,meta in values.items():
            he,en=aliases.get(imdb,('',''))
            names=[meta.get('name'),meta.get('originalName'),he,en]
            if years and str(meta.get('releaseInfo',''))[:4] not in years:continue
            if words and not any(all(any(t.startswith(w) for t in self.search_words(name)) for w in words) for name in names):continue
            meta={**meta,'id':imdb,'type':kind}
            if he:meta['name']=he+' / '+str(meta.get('name') or en or '')
            meta.setdefault('poster',f'https://images.metahub.space/poster/medium/{imdb}/img')
            output.append(meta)
        return sorted(output,key=lambda m:(-self.search_rank(m,query),str(m.get('name','')).casefold(),m['id']))

    def canonical_movie_year(self,kind,imdb,meta):
        # Provider regional-release years can differ from the same IMDb film's
        # indexed year. Keep its real release date/month and all identity data.
        path=self.g['DATA']/'imdb/catalogue.sqlite'
        if kind!='movie' or not path.is_file():return meta
        with closing(sqlite3.connect('file:'+str(path)+'?mode=ro',uri=True)) as c:
            row=c.execute("SELECT year FROM titles WHERE kind='movie' AND imdb=?",(imdb,)).fetchone()
        if row and isinstance(row[0],int) and 1800<=row[0]<=dt.date.today().year+10:
            return {**meta,'releaseInfo':str(row[0])}
        return meta

    def search(self,kind,query):
        query=query.strip()
        if re.fullmatch(r'(19|20)\d{2}-\d{2}(?:\s*\.\.\s*(19|20)\d{2}-\d{2})?',query):
            return self.browse(kind,0,1000,month=query)['metas']
        if re.fullmatch(r'(19|20)\d{2}(?:\s*[-–—]\s*(19|20)\d{2})?',query):
            return self.browse(kind,0,1000,query)['metas']
        if re.search('[\u0590-\u05ff]',query):
            try:raw=self.hebrew_search(kind,query)
            except Exception:raw=[];self.log('Hebrew title lookup deferred; using known local aliases')
        else:
            search_cache='search-meta:'+kind+':'+hashlib.sha256(query.casefold().encode()).hexdigest()
            raw=self.cached(search_cache)
            if raw is None:
                try:
                    raw=self.fetch(f"{self.g['CINEMETA']}/catalog/{kind}/top/search={U.quote(query,safe='')}.json",timeout=3).get('metas',[])
                    self.remember(search_cache,raw,21600)
                except Exception:
                    raw=[];self.remember(search_cache,raw,300)
        canonical=self.ready_search(kind,query)
        local=self.index_search(kind,query);seen=set();output=[]
        if re.search('[\u0590-\u05ff]',query):
            for keyword in (self.cached('hebrew-keywords:'+self.search_key(query)) or [])[:5]:
                local+=self.index_search(kind,keyword)
        self.decorate_posters(canonical+raw+local)
        self.enqueue_many((kind+':'+meta.get('id','')+(':1:1' if kind=='series' else '') for meta in raw+local),100)
        ready=self.ready_titles()
        for meta in canonical+raw+local:
            imdb=meta.get('id')
            if not imdb or imdb in seen:continue
            if (meta.get('released') or '')[:10]>dt.date.today().isoformat():continue
            if kind+':'+imdb not in ready:continue
            seen.add(imdb);output.append(self.localize(meta))
        return self.decorate_posters(sorted(output,key=lambda m:(-self.search_rank(m,query),str(m.get('name','')).casefold(),m['id'])))

    def repair_stub_title(self,imdb,meta):
        # Read the local native index only; all metadata writes use Jellyfin's
        # supported API. Preserve the full existing DTO and never send UserData.
        path=self.media.parent/'appdata/jellyfin/config/data/jellyfin.db'
        if not path.is_file():return
        with closing(sqlite3.connect('file:'+str(path)+'?mode=ro',uri=True,timeout=1)) as native:
            row=native.execute("SELECT Id,Name FROM BaseItems WHERE Path=? AND Type='MediaBrowser.Controller.Entities.TV.Series'",('/catalogue/shows/'+imdb,)).fetchone()
        if not row or row[1]!=imdb:return
        users=self.jf('/Users',timeout=5)
        user=next((u for u in users if u.get('Policy',{}).get('IsAdministrator')),users[0] if users else None)
        if not user:raise RuntimeError('No native user for series metadata repair')
        dto=self.jf('/Users/'+user['Id']+'/Items/'+row[0],timeout=5)
        if dto.get('Name')!=imdb:return
        dto=dict(dto);dto.pop('UserData',None)
        dto['Name']=meta['name']
        dto['ProviderIds']={**(dto.get('ProviderIds') or {}),'Imdb':imdb}
        if not dto.get('Overview'):dto['Overview']=meta.get('description')
        if not dto.get('ProductionYear') and str(meta.get('releaseInfo',''))[:4].isdigit():dto['ProductionYear']=int(str(meta['releaseInfo'])[:4])
        if dto.get('Tags') is None:dto['Tags']=[]
        self.jf('/Items/'+row[0],dto,method='POST',timeout=5)

    def repair_cached_series(self,limit=2,only=None):
        """Hydrate poster-created native series stubs using saved metadata only.

        Video readiness stays independent. Creating released STRM metadata does
        not mark unchecked episodes playable or change native user data.
        """
        if self.foreground_busy():return []
        root=self.g['LIBRARY']/'shows'
        if not root.is_dir():return []
        with self.db() as conn:
            ready={key.split(':')[1] for key, in conn.execute("SELECT key FROM home_ready WHERE key LIKE 'series:%'")}
        repaired=[];attempted=0
        folders=[root/only] if only and re.fullmatch(r'tt[0-9]{5,12}',only) else sorted(root.iterdir()) if only is None else []
        for folder in folders:
            imdb=folder.name
            if attempted>=limit or self.foreground_busy():break
            if not folder.is_dir() or folder.is_symlink() or imdb not in ready or not re.fullmatch(r'tt[0-9]{5,12}',imdb):continue
            with self.db() as conn:
                row=conn.execute('SELECT meta FROM items WHERE key=?',('series:'+imdb,)).fetchone()
                saved=conn.execute('SELECT value FROM cache WHERE key=?',('meta:series:'+imdb,)).fetchone()
            pending='series-metadata-refresh:'+imdb
            needs=not row or not (folder/'tvshow.nfo').is_file()
            if not needs and not self.cached(pending):continue
            meta=json.loads(saved[0]) if saved else json.loads(row[0]) if row else {}
            if meta.get('id',meta.get('imdb_id'))!=imdb or not meta.get('videos') or not meta.get('name') or meta['name']==imdb:continue
            if not any(isinstance(v.get('season'),int) and isinstance(v.get('episode'),int) and v['episode']>0 and v.get('released') and v['released'][:10]<=dt.date.today().isoformat() for v in meta['videos']):continue
            attempted+=1
            try:
                if needs:
                    self.g['import_item']('series',meta,'Metadata recovery')
                    self.remember(pending,True,30*86400)
                # A metadata refresh does not discover new children. Notify
                # the native directory monitor to scan this exact series folder.
                self.jf('/Library/Media/Updated',{'Updates':[{'Path':'/catalogue/shows/'+imdb}]},method='POST',timeout=5)
                self.repair_stub_title(imdb,meta)
                with self.db() as conn:conn.execute('DELETE FROM cache WHERE key=?',(pending,))
                repaired.append(imdb)
                self.log('Cached series metadata repaired: '+imdb)
            except Exception as error:self.log('Series metadata repair deferred: '+imdb+' ('+type(error).__name__+')')
        return repaired

    def verification_held(self,key):
        with self.db() as conn:
            return bool(conn.execute('SELECT 1 FROM verification_holds WHERE key=? AND expires>?',(key,time.time())).fetchone())

    def claim_verification(self,key,owner):
        token=uuid.uuid4().hex
        with self.db() as conn:
            conn.execute('BEGIN IMMEDIATE')
            conn.execute('DELETE FROM verification_claims WHERE expires<=?',(time.time(),))
            cursor=conn.execute('INSERT OR IGNORE INTO verification_claims VALUES (?,?,?,?)',(key,token,owner,time.time()+30))
            return token if cursor.rowcount else None

    def renew_verification(self,key,token):
        # A busy writer must not immediately discard an otherwise valid remote
        # operation. Retry briefly, bounded by both claim expiry and job budget.
        started=time.monotonic();deadline=started+5;retries=0
        budget=self.g.get('BUDGET')
        for name in ('deadline','worker_deadline'):
            bound=getattr(budget,name,None)
            if bound is not None:deadline=min(deadline,bound)
        with self.g['db']() as conn:
            row=conn.execute('SELECT expires FROM verification_claims WHERE key=? AND token=?',(key,token)).fetchone()
            if not row or row[0]<=time.time():return False
            deadline=min(deadline,time.monotonic()+max(0,row[0]-time.time()))
            while True:
                left=deadline-time.monotonic()
                if left<=0:
                    raise sqlite3.OperationalError('Verification renewal wait budget exhausted')
                conn.execute('PRAGMA busy_timeout='+str(max(1,min(250,int(left*1000)))))
                try:
                    conn.execute('BEGIN IMMEDIATE')
                    # Recheck expiry after obtaining the writer lock. Never
                    # resurrect an expired claim or replace another owner.
                    now=time.time()
                    if time.monotonic()>=deadline:
                        conn.rollback();return False
                    cursor=conn.execute('UPDATE verification_claims SET expires=? WHERE key=? AND token=? AND expires>?',(now+30,key,token,now))
                    renewed=bool(cursor.rowcount)
                    conn.commit()
                    if retries:self.log('Verification claim renewal '+json.dumps({'retries':retries,'wait_seconds':round(time.monotonic()-started,3),'renewed':renewed}))
                    return renewed
                except sqlite3.OperationalError as error:
                    conn.rollback()
                    code=getattr(error,'sqlite_errorcode',0) or 0
                    if code & 255 not in (sqlite3.SQLITE_BUSY,sqlite3.SQLITE_LOCKED):raise
                    retries+=1
                    if time.monotonic()>=deadline:
                        self.log('Verification claim renewal '+json.dumps({'retries':retries,'wait_seconds':round(time.monotonic()-started,3),'renewed':False}))
                        raise
                    time.sleep(min(.05,max(0,deadline-time.monotonic())))

    def release_verification(self,key,token):
        with self.db() as conn:conn.execute('DELETE FROM verification_claims WHERE key=? AND token=?',(key,token))

    def worker_audit(self,recurring=False):
        # Holds are scheduling exclusions, not failed/finished audit results.
        # Keep original proof/readiness/history and automatically retry after expiry.
        with self.g['REL'].db() as conn:
            row=conn.execute("SELECT a.key FROM verification_audit a JOIN validation v ON v.key=a.key WHERE a.completed=0 AND (v.state='verified' OR v.next_check<=?) AND NOT EXISTS(SELECT 1 FROM verification_claims vc WHERE vc.key=v.key AND vc.expires>unixepoch()) AND NOT EXISTS(SELECT 1 FROM verification_holds h WHERE h.key=v.key AND h.expires>unixepoch()) AND NOT EXISTS(SELECT 1 FROM metadata_waits mw WHERE mw.expires>unixepoch() AND (v.key=mw.key OR v.key LIKE mw.key||':%')) ORDER BY a.priority DESC,COALESCE(json_extract(v.proof,'$.version'),0),a.scheduled LIMIT 1",(time.time(),)).fetchone()
            if not row and recurring:
                row=conn.execute("SELECT key FROM validation WHERE state='verified' AND checked<=? AND NOT EXISTS(SELECT 1 FROM verification_claims vc WHERE vc.key=validation.key AND vc.expires>unixepoch()) AND NOT EXISTS(SELECT 1 FROM verification_holds h WHERE h.key=validation.key AND h.expires>unixepoch()) AND NOT EXISTS(SELECT 1 FROM metadata_waits mw WHERE mw.expires>unixepoch() AND (validation.key=mw.key OR validation.key LIKE mw.key||':%')) ORDER BY checked LIMIT 1",(time.time()-86400,)).fetchone()
        return row

    def warm_availability(self,limit=20,remote=False,slot=1):
        if remote:
            from verification_remote import ready,jobs
            status=jobs(self.g).status()
            if (not ready(self.g) or status.get('windows_slots_online',0)<min(slot,status.get('windows_capacity',1))
                or status.get('windows_queue_capacity',status.get('windows_capacity',1))<slot):return
        worker_key=('remote-validation-worker' if slot==1 else 'remote-validation-worker-'+str(slot)) if remote else 'validation-worker'
        lock_name=('remote-validation.lock' if slot==1 else 'remote-validation-'+str(slot)+'.lock') if remote else 'validation.lock'
        self.g['BUDGET'].remote_verification=remote
        self.g['BUDGET'].remote_slot=slot
        self.consume_rechecks()
        # flock survives separate cron/docker exec processes. Interrupted rows
        # remain due and resume automatically; no in-memory-only work cursor.
        with (self.g['DATA']/lock_name).open('a') as lock:
            try:fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
            except BlockingIOError:return
            # Defer all torrent probes while the owner is playing anything.
            def busy():
                if self.foreground_busy():return 'Yielding briefly to Jellyfin navigation'
                try:
                    deadline=min(time.monotonic()+1,getattr(self.g['BUDGET'],'deadline',None) or float('inf'))
                    with self.g['request_budget'](deadline,None):
                        return 'Paused while Jellyfin is playing' if any(s.get('NowPlayingItem') for s in self.jf('/Sessions',timeout=1)) else ''
                except Exception:return 'Waiting for Jellyfin startup or session status'
            blocked=busy()
            if blocked:
                self.remember(worker_key,{'message':blocked+'; resumes automatically','started':time.time()},180)
                return
            if self.cached('validation-provider-backoff'):
                self.remember(worker_key,{'message':'Source provider cooldown; resumes automatically','started':time.time()},180)
                return
            if not remote:
                # If the guarded extra server lane is unavailable, let the
                # existing server worker perform one Russian-audio scan while
                # Windows workers are actively contributing. It then returns
                # to the normal V6 loop; Windows-off server behavior is intact.
                try:
                    from verification_remote import jobs
                    from russian_audio import RussianAudio
                    queue=jobs(self.g);status=queue.status() if queue else {}
                    if (status.get('windows_slots_online',0)>0
                        and queue is not None
                        and not queue.setting_read('russian_extra_permitted',False)
                        and queue.setting_read('russian_serial_permitted',False)):
                        RussianAudio(self.g).work('serial')
                except (OSError,ValueError,sqlite3.Error):
                    self.log('Russian-audio serial fallback deferred')
            self.seed_validation()
            self.g['REL'].schedule()
            self.start_verification_sweep()
            self.repair_cached_series(limit=2)
            batch_number=0;queue=None
            if remote:
                try:
                    from verification_remote import jobs
                    queue=jobs(self.g)
                    queue.count('windows_batches_started')
                    batch_number=queue.status()['totals'].get('windows_batches_started',0)
                except sqlite3.Error:self.log('Windows batch counter deferred')
            started=time.monotonic();checked=0
            local_title_mode=bool(remote and queue and queue.setting_read('local_torrent_enabled',False) and queue.local_workers()) if remote else False
            if local_title_mode:limit=1
            self.g['BUDGET'].worker_deadline=started+(960 if local_title_mode else 480)
            slot=int((self.cached('verification-v5-slot') or {}).get('slot',0))
            while checked<limit and time.monotonic()-started<480:
                blocked=busy()
                if blocked:
                    self.remember(worker_key,{'message':blocked+'; resumes automatically','started':time.time()},180)
                    break
                sweep_candidate,sweep_waiting=self.first_sweep_candidate()
                with self.db() as conn:
                    row=conn.execute("SELECT v.key FROM manual_rechecks m JOIN validation v ON v.key=m.key WHERE m.status='pending' AND v.next_check<=? AND NOT EXISTS(SELECT 1 FROM verification_claims vc WHERE vc.key=v.key AND vc.expires>unixepoch()) AND NOT EXISTS(SELECT 1 FROM verification_holds h WHERE h.key=v.key AND h.expires>unixepoch()) AND NOT EXISTS(SELECT 1 FROM metadata_waits mw WHERE mw.expires>unixepoch() AND (v.key=mw.key OR v.key LIKE mw.key||':%')) ORDER BY m.requested LIMIT 1",(time.time(),)).fetchone()
                    if not row and sweep_candidate:row=(sweep_candidate[1],)
                    # Initial audits precede discovery; one in five ordinary
                    # slots subsequently rechecks a success older than 24 hours.
                    if not row and not sweep_waiting:row=self.worker_audit(recurring=slot%5==4)
                    if not row and not sweep_waiting:
                        work=conn.execute("SELECT w.key,w.value FROM preparation_work w JOIN validation v ON v.key=w.key WHERE (v.state='verified' OR v.next_check<=?) AND NOT EXISTS(SELECT 1 FROM verification_claims vc WHERE vc.key=v.key AND vc.expires>unixepoch()) AND NOT EXISTS(SELECT 1 FROM verification_holds h WHERE h.key=v.key AND h.expires>unixepoch()) AND NOT EXISTS(SELECT 1 FROM metadata_waits mw WHERE mw.expires>unixepoch() AND (v.key=mw.key OR v.key LIKE mw.key||':%')) ORDER BY w.updated LIMIT 1",(time.time(),)).fetchone()
                        if work:row=(work[0],)
                    # Reserve one in five probes for due failures so a large
                    # discovery backlog cannot starve the saved retry list.
                    if not row and not sweep_waiting and checked%5==0:
                        row=conn.execute("SELECT key FROM validation WHERE state='unavailable' AND next_check<=? AND NOT EXISTS(SELECT 1 FROM verification_claims vc WHERE vc.key=validation.key AND vc.expires>unixepoch()) AND NOT EXISTS(SELECT 1 FROM verification_holds h WHERE h.key=validation.key AND h.expires>unixepoch()) AND NOT EXISTS(SELECT 1 FROM metadata_waits mw WHERE mw.expires>unixepoch() AND (validation.key=mw.key OR validation.key LIKE mw.key||':%')) ORDER BY next_check,priority DESC LIMIT 1",(time.time(),)).fetchone()
                    if not row and not sweep_waiting:
                        # Most slots widen the catalogue; one in five grows
                        # episode coverage of shows already visible.
                        breadth="(key LIKE 'movie:%' OR key LIKE 'series:%:1:1')"
                        wanted="NOT "+breadth if checked%5==4 else breadth
                        row=conn.execute("SELECT key FROM validation WHERE state!='verified' AND next_check<=? AND NOT EXISTS(SELECT 1 FROM verification_claims vc WHERE vc.key=validation.key AND vc.expires>unixepoch()) AND NOT EXISTS(SELECT 1 FROM verification_holds h WHERE h.key=validation.key AND h.expires>unixepoch()) AND NOT EXISTS(SELECT 1 FROM metadata_waits mw WHERE mw.expires>unixepoch() AND (validation.key=mw.key OR validation.key LIKE mw.key||':%')) AND "+wanted+" ORDER BY priority DESC,next_check,rowid LIMIT 1",(time.time(),)).fetchone()
                    if not row and not sweep_waiting:
                        row=conn.execute("SELECT key FROM validation WHERE state!='verified' AND next_check<=? AND NOT EXISTS(SELECT 1 FROM verification_claims vc WHERE vc.key=validation.key AND vc.expires>unixepoch()) AND NOT EXISTS(SELECT 1 FROM verification_holds h WHERE h.key=validation.key AND h.expires>unixepoch()) AND NOT EXISTS(SELECT 1 FROM metadata_waits mw WHERE mw.expires>unixepoch() AND (validation.key=mw.key OR validation.key LIKE mw.key||':%')) ORDER BY priority DESC,next_check,rowid LIMIT 1",(time.time(),)).fetchone()
                if not row:break
                key=row[0]
                if self.verification_held(key):continue
                claim=self.claim_verification(key,'windows' if remote else 'server')
                if not claim:continue
                sweep_attempt=bool(sweep_candidate and sweep_candidate[1]==key and self.mark_first_sweep_attempt(key))
                budget=self.g['BUDGET'];previous_sweep_flag=getattr(budget,'first_sweep_attempt',False)
                previous_attempt_deadline=getattr(budget,'worker_deadline',None)
                budget.first_sweep_attempt=sweep_attempt
                if sweep_attempt:budget.worker_deadline=min(previous_attempt_deadline or time.monotonic()+240,time.monotonic()+240)
                sweep_outcome='deferred'
                self.remember(worker_key,{'key':key,'started':time.time(),'position':checked+1,'batch':batch_number,'message':'Video sample validation running'},180)
                attempt_started=time.time()
                slot+=1;self.remember('verification-v5-slot',{'slot':slot},10*365*86400)
                try:
                    # Retry only when due, with fresh source listings. A cached
                    # negative listing must not poison its own next retry.
                    with self.db() as conn:
                        conn.execute("DELETE FROM cache WHERE key=? OR key LIKE ?",('prepared:'+key,'prepared:'+key+':%'))
                    with self.g['REL'].db() as conn:work=conn.execute('SELECT value FROM preparation_work WHERE key=?',(key,)).fetchone()
                    audio_index=json.loads(work[0]).get('audio_index') if work else None
                    last_session_check=[0,False]
                    budget.remote_cancel_code=''
                    last_claim_renew=[time.monotonic()]
                    def interrupted():
                        if time.monotonic()-last_claim_renew[0]>=10:
                            if not self.renew_verification(key,claim):
                                budget.remote_cancel_code='title_claim_lost';return True
                            last_claim_renew[0]=time.monotonic()
                            if remote:
                                from verification_remote import producer_phase
                                producer_phase(self.g,key)
                        if remote:
                            from verification_remote import ready
                            if not ready(self.g):
                                budget.remote_cancel_code='worker_unavailable';return True
                        if self.foreground_busy():
                            budget.remote_cancel_code='navigation_interrupted';return True
                        if time.monotonic()-last_session_check[0]>=2:
                            reason=busy()
                            last_session_check[:]=[time.monotonic(),bool(reason)]
                            if reason:budget.remote_cancel_code='playback_interrupted' if reason.startswith('Paused while Jellyfin is playing') else 'navigation_interrupted' if reason.startswith('Yielding') else 'session_unavailable'
                        return last_session_check[1]
                    handled=False
                    if remote and audio_index is None:
                        from verification_remote import jobs
                        local_queue=jobs(self.g)
                        if local_queue and local_queue.setting_read('local_torrent_enabled',False) and local_queue.local_workers():
                            from verification_title import execute_title
                            handled=execute_title(self.g,key,interrupted)
                    if not handled:
                        self.g['prepared'](key,background=True,**({'audio_index':audio_index} if audio_index is not None else {}),cancel=interrupted)
                    if sweep_attempt:sweep_outcome='passed'
                    self.clear_verification_retry(key)
                    if remote:
                        from verification_remote import jobs
                        jobs(self.g).count('windows_titles_verified')
                    if key.startswith('series:'):
                        try:
                            meta=self.g['metadata']('series',key.split(':')[1])
                            aired=[]
                            for episode in meta.get('videos',[]):
                                season=episode.get('season',0);number=episode.get('episode',0)
                                if season>0 and number>0 and (episode.get('released') or '9999')[:10]<=dt.date.today().isoformat():
                                    aired.append(f'series:{key.split(":")[1]}:{season}:{number}')
                            self.enqueue_many(aired,30)
                        except Exception:self.log('Episode verification queue expansion deferred')
                except Exception as error:
                    self.log('Verification attempt interrupted ('+type(error).__name__+')')
                    # A lease held by the metadata PC is a scheduling wait, not
                    # a source failure. Its short SQL exclusion lets this batch
                    # continue checking other titles rather than stalling.
                    pending=self.g.get('MetadataPending')
                    if pending and isinstance(error,pending):
                        checked+=1
                        self.remember(worker_key,{'message':'Metadata pending on helper; checking other titles','started':time.time(),'batch':batch_number},180)
                        continue
                    transient=tuple(self.g[name] for name in ('ClientCancelled','PlaybackBusy','Checkpoint') if isinstance(self.g.get(name),type))
                    checkpoint=self.g.get('Checkpoint')
                    if checkpoint and isinstance(error,checkpoint) and str(error).startswith('Torrent metadata pending'):
                        # A swarm whose file list is still loading is unknown,
                        # not unavailable. Cool down just this title so the
                        # same producer can feed another ready candidate.
                        with self.db() as conn:conn.execute('INSERT OR REPLACE INTO metadata_waits VALUES (?,?)',(key,time.time()+60))
                        if remote:
                            from verification_remote import jobs
                            jobs(self.g).count('windows_title_checkpoints')
                        self.remember(worker_key,{'message':'Torrent metadata pending; moving to another title','started':time.time(),'batch':batch_number},180)
                        checked+=1
                        continue
                    if getattr(error,'failure_category',None):
                        self.defer_verification(key,error.failure_category)
                        if remote:
                            from verification_remote import jobs
                            jobs(self.g).count('windows_title_checkpoints')
                        checked+=1
                        continue
                    if isinstance(error,sqlite3.OperationalError) or transient and isinstance(error,transient):
                        # Cache-only attribution waits must not monopolize all
                        # producers by immediately reclaiming the same title.
                        if not isinstance(error,sqlite3.OperationalError):
                            with self.db() as conn:
                                wait=conn.execute("SELECT 1 FROM preparation_work WHERE key=? AND json_extract(value,'$.speed_status')='cache_wait'",(key,)).fetchone()
                                if wait:conn.execute('INSERT OR REPLACE INTO metadata_waits VALUES (?,?)',(key,time.time()+300))
                        if remote:
                            from verification_remote import jobs
                            jobs(self.g).count('windows_title_checkpoints')
                        self.remember(worker_key,{'message':'Work checkpointed; resumes after foreground activity or next batch','started':time.time(),'batch':batch_number},180)
                        break
                    if isinstance(error,urllib.error.HTTPError) and (error.code==429 or error.code>=500):
                        self.remember('validation-provider-backoff',True,900 if error.code==429 else 300)
                        self.log('Validation paused for source provider cooldown')
                        break
                    from reliability import SourceFailure
                    with self.db() as conn:current=conn.execute('SELECT checked,proof FROM validation WHERE key=?',(key,)).fetchone()
                    if isinstance(error,urllib.error.URLError) or (current and current[1] and not isinstance(error,SourceFailure)):
                        # An HTTP/database/unknown request fault is not a new
                        # negative media proof. Keep a requested fresh check
                        # pending and let other eligible titles make progress.
                        with self.db() as conn:conn.execute('INSERT OR REPLACE INTO metadata_waits VALUES (?,?)',(key,time.time()+30))
                        if remote:
                            from verification_remote import jobs
                            jobs(self.g).count('windows_title_checkpoints')
                        self.remember(worker_key,{'message':'Request fault checkpointed; source proof retained','started':time.time(),'batch':batch_number},180)
                        break
                    if not current or current[0]<time.time()-30:
                        if self.validation_result(key,False,reason=type(error).__name__,attempt_started=attempt_started,background=True) and sweep_attempt:sweep_outcome='failed'
                    if remote:
                        from verification_remote import jobs
                        jobs(self.g).count('windows_titles_unavailable')
                finally:
                    if sweep_attempt:self.finish_first_sweep_attempt(key,sweep_outcome)
                    budget.first_sweep_attempt=previous_sweep_flag
                    if sweep_attempt:budget.worker_deadline=previous_attempt_deadline
                    self.release_verification(key,claim)
                    if remote:
                        from verification_remote import producer_phase
                        producer_phase(self.g,key,'finished')
                checked+=1
                self.remember(worker_key,{'key':key,'started':time.time(),'position':checked,'batch':batch_number,'message':'Last probe finished; proceeding to next queue entry'},180)
                # Remote work is already bounded by exclusive queue leases and
                # provider backoff. Dispatch the next title without another
                # fixed idle gap; the loop rechecks viewing/cancellation first.
                if not remote:time.sleep(2)
            self.log(f'Persistent video validation: {checked} checked; unavailable entries retained with backoff')

    def sync_discovery(self):
        self.update_index()
        self.seed_validation()
        self.sync_release_dates(max_pages=100)
        with self.db() as conn:
            conn.execute("DELETE FROM cache WHERE key LIKE 'discover-page:%' OR key LIKE 'browse:%'")
            rows=conn.execute("SELECT imdb,meta FROM items WHERE kind='series'").fetchall()
        for kind in ('movie','series'):
            for year in (dt.date.today().year,dt.date.today().year-1):
                try:self.page(kind,year,0)
                except Exception:self.log(f'Recent discovery provider page deferred: {kind} {year}')
        with self.db() as conn:recent=conn.execute('SELECT kind,imdb,meta FROM discovery').fetchall()
        # The index importer can commit while this scheduled refresh adds
        # discovered titles. Wait for that bounded writer instead of failing
        # the whole refresh on SQLite's short default busy timeout.
        with closing(sqlite3.connect(self.g['DATA']/'imdb/catalogue.sqlite',timeout=60)) as index:
            for kind,imdb,raw in recent:
                meta=json.loads(raw);year=str(meta.get('releaseInfo',''))[:4]
                if year.isdigit():
                    votes=1 if meta.get('released') and meta['released'][:10]<=dt.date.today().isoformat() else 0
                    inserted=index.execute('INSERT OR IGNORE INTO titles(imdb,kind,name,original,year,genres,votes) VALUES (?,?,?,?,?,?,?)',
                        (imdb,kind,meta.get('name',imdb),meta.get('originalName',meta.get('name',imdb)),int(year),','.join(meta.get('genres') or []),votes))
                    if inserted.rowcount:
                        index.execute('INSERT INTO title_fts(rowid,name,original) SELECT rowid,name,original FROM titles WHERE imdb=?',(imdb,))
            index.commit()
        changed=0
        for imdb,raw in rows:
            try:
                # The helper supplies the normal six-hour metadata cache. Do
                # not invalidate fresh leased results during routine refresh.
                if not (self.g['DATA']/'metadata-helper.enabled').is_file():
                    with self.db() as conn:conn.execute('DELETE FROM cache WHERE key=?',('meta:series:'+imdb,))
                meta=self.g['metadata']('series',imdb)
                old=json.loads(raw)
                if meta.get('videos')!=old.get('videos'):
                    self.g['import_item']('series',meta,'Opened');changed+=1
                time.sleep(.15)
            except Exception:self.log('Episode refresh deferred for '+imdb)
        if changed:self.jf('/Library/Refresh',{},method='POST')
        if not self.foreground_busy():
            try:self.browse('all',limit=1)
            except Exception:self.log('Compact catalogue warm-up deferred')
        self.refresh_hebrew_metadata(limit=8)
        self.log(f'Discovery pages refreshed; {changed} shows gained updated episode metadata')

    def records(self):
        with self.db() as conn:return {key:json.loads(value) for key,value in conn.execute('SELECT key,value FROM downloads')}

    @contextmanager
    def operation_lock(self):
        self.g['DATA'].mkdir(parents=True,exist_ok=True)
        with (self.g['DATA']/'managed-downloads.lock').open('a+') as lock:
            fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
            try:yield
            finally:fcntl.flock(lock,fcntl.LOCK_UN)

    def save_record(self,key,value):
        with self.db() as conn:conn.execute('INSERT OR REPLACE INTO downloads VALUES (?,?)',(key,json.dumps(value)))

    def local_download(self,key):
        with self.db() as conn:row=conn.execute('SELECT value FROM downloads WHERE key=?',(key,)).fetchone()
        if not row:return None
        record=json.loads(row[0])
        path=Path(record.get('path',''))
        if record.get('state')!='ready' or not path.is_file():return None
        if not any(path.resolve().is_relative_to(self.media/folder/'Saved') for folder in ('movies','shows')):
            raise ValueError('Invalid managed download path')
        return {**record,'local':str(path),'filename':record.get('filename',path.name),'length':path.stat().st_size}

    def transmission(self,method,arguments):
        url='http://transmission:9091/transmission/rpc'
        headers=self.g['torr_headers']()
        token=self.cached('transmission-session')
        if token:headers['X-Transmission-Session-Id']=token
        for _ in range(2):
            try:
                result=self.fetch(url,{'method':method,'arguments':arguments},headers,timeout=10)
                if result.get('result')!='success':raise RuntimeError('Download manager rejected request')
                return result.get('arguments',{})
            except urllib.error.HTTPError as error:
                if error.code!=409:raise
                token=error.headers.get('X-Transmission-Session-Id')
                headers['X-Transmission-Session-Id']=token
                self.remember('transmission-session',token,3600)
        raise RuntimeError('Download manager session failed')

    def queue_download(self,key,selected):
        with self.download_lock,self.operation_lock():
            records=self.records()
            old=records.get(key)
            if old and (old.get('state') in ('queued','requesting') or old.get('state')=='ready' and self.local_download(key)):return old
            if len([v for v in records.values() if v.get('state') in ('queued','requesting')])>=3:
                raise RuntimeError('Three downloads already queued')
            free=shutil.disk_usage(self.media).free
            reserved=sum(v.get('length',0) for v in records.values() if v.get('state')=='queued')
            if selected['length']+reserved>free-20*1024**3:raise RuntimeError('Not enough free media space')
            wanted=selected['index']-1
            infohash=selected['hash']
            known=any(v.get('hash')==infohash for v in records.values())
            existing=self.transmission('torrent-get',{'ids':[infohash],'fields':['hashString','labels','downloadDir']}).get('torrents',[])
            if existing and (not known or 'home-media-managed' not in existing[0].get('labels',[])
                             or existing[0].get('downloadDir')!='/downloads/complete/Saved/'+infohash):
                raise RuntimeError('This torrent is already managed outside this feature')
            directory=self.downloads/'Saved'/infohash
            if directory.exists() and not known:raise RuntimeError('Download folder already exists; refusing to reuse existing data')
            if (directory/selected['filename']).is_file() and not existing:raise RuntimeError('Requested download path already contains data')
            args={'filename':selected.get('torrent_url') or 'magnet:?xt=urn:btih:'+infohash,'download-dir':'/downloads/complete/Saved/'+infohash,
                  'paused':False,'files-wanted':[wanted],
                  'files-unwanted':[i for i in range(selected.get('file_count') or len(self.g['torr']({'action':'get','hash':infohash}).get('file_stats',[]))) if i!=wanted],
                  'labels':['home-media-managed']}
            record={'state':'requesting','hash':infohash,'index':wanted,'filename':selected['filename'],
                    'length':selected['length'],'requested_at':time.time(),'relative_dir':infohash}
            self.save_record(key,record)
            result={} if existing else self.transmission('torrent-add',args)
            if existing or result.get('torrent-duplicate'):
                if not known:raise RuntimeError('This torrent is already managed outside this feature')
                self.transmission('torrent-set',{'ids':[infohash],'files-wanted':[wanted]})
                self.transmission('torrent-start',{'ids':[infohash]})
            record['state']='queued'
            self.save_record(key,record)
            return record

    def manage_downloads(self):
        try:
            with self.operation_lock():return self._manage_downloads()
        except BlockingIOError:
            self.log('Download maintenance deferred while a save request is active')

    def _manage_downloads(self):
        records=self.records()
        for key,record in records.items():
            if record.get('state')=='moving':
                self.finish_move(key,record,records)
                continue
            if record.get('state') not in ('queued','requesting'):continue
            torrents=self.transmission('torrent-get',{'ids':[record['hash']],
                'fields':['hashString','files','fileStats','downloadDir','error','errorString']}).get('torrents',[])
            if not torrents:
                if record['state']=='requesting' and time.time()-record['requested_at']>60:
                    record['state']='failed';self.save_record(key,record)
                continue
            if record['state']=='requesting':
                record['state']='queued';self.save_record(key,record)
            torrent=torrents[0];files=torrent.get('files',[]);index=record['index']
            if index>=len(files):continue
            file=files[index]
            if file.get('bytesCompleted',0)<file.get('length',1):continue
            source=self.downloads/'Saved'/record.get('relative_dir','')/file['name']
            if not source.resolve().is_relative_to(self.downloads/'Saved') or source.is_symlink():
                raise ValueError('Invalid completed download path')
            if not source.is_file():continue
            if source.stat().st_size!=file['length']:raise RuntimeError('Completed source size changed; move deferred')
            kind,imdb,*numbers=key.split(':')
            meta=self.g['metadata'](kind,imdb)
            parent=self.media/('movies' if kind=='movie' else 'shows')/'Saved'/imdb
            if numbers:parent=parent/('Season '+str(int(numbers[0])).zfill(2))
            parent.mkdir(parents=True,exist_ok=True)
            stem='movie' if kind=='movie' else f'S{int(numbers[0]):02d}E{int(numbers[1]):02d}'
            target=parent/(stem+source.suffix.lower())
            if target.exists():raise RuntimeError('Managed destination already exists; refusing overwrite')
            self.transmission('torrent-stop',{'ids':[record['hash']]})
            self.transmission('torrent-set',{'ids':[record['hash']],'files-unwanted':[index]})
            record.update(state='moving',path=str(target),move_source=str(source),
                          source_inode=source.stat().st_ino,source_size=source.stat().st_size,
                          move_meta={'name':meta.get('name'),'releaseInfo':meta.get('releaseInfo')})
            # Persist intent before the atomic rename. A power loss between the
            # filesystem operation and the ready record can be resumed safely.
            self.save_record(key,record)
            self.finish_move(key,record,records)
        self.cleanup_saved()

    def finish_move(self,key,record,records):
        source=Path(record['move_source']);target=Path(record['path'])
        kind,imdb,*numbers=key.split(':');meta=record.get('move_meta',{})
        if not source.resolve().is_relative_to(self.downloads/'Saved') or source.is_symlink():
            raise ValueError('Invalid recovery source')
        if not target.resolve().is_relative_to(self.media/('movies' if kind=='movie' else 'shows')/'Saved') or target.is_symlink():
            raise ValueError('Invalid recovery destination')
        if target.exists():
            if source.exists() or target.stat().st_ino!=record['source_inode'] or target.stat().st_size!=record['source_size']:
                raise RuntimeError('Move recovery refuses to overwrite changed data')
        else:
            if not source.is_file() or source.stat().st_ino!=record['source_inode']:
                raise RuntimeError('Move recovery source changed')
            source.rename(target)
        sidecars=record.get('sidecars',[])
        for lang,short in [('heb','he'),('eng','en')]:
            sub=self.g['DATA']/'subtitles'/hashlib.sha256(key.encode()).hexdigest()[:24]/(short+'.srt')
            if sub.is_file():
                dest=target.with_suffix('.'+lang+'.srt')
                if not dest.exists():shutil.copyfile(sub,dest);sidecars.append(str(dest))
        nfo_path=target.with_suffix('.nfo')
        if not nfo_path.exists():
            self.g['nfo'](nfo_path,'movie' if kind=='movie' else 'episodedetails',
                {'title':meta.get('name'),'year':str(meta.get('releaseInfo') or '')[:4],
                 'imdbid':imdb if kind=='movie' else None,'season':numbers[0] if numbers else None,
                 'episode':numbers[1] if numbers else None})
            sidecars.append(str(nfo_path))
        if numbers:
            show_nfo=self.media/'shows'/'Saved'/imdb/'tvshow.nfo'
            if not show_nfo.exists():self.g['nfo'](show_nfo,'tvshow',{'title':meta.get('name'),'imdbid':imdb})
        record.update(state='ready',sidecars=sidecars,completed_at=time.time(),
                      size=target.stat().st_size,inode=target.stat().st_ino,mtime_ns=target.stat().st_mtime_ns)
        self.save_record(key,record);records[key]=record
        if any(v.get('hash')==record['hash'] and v.get('state') in ('queued','requesting','moving') for v in records.values()):
            self.transmission('torrent-start',{'ids':[record['hash']]})
        else:self.transmission('torrent-remove',{'ids':[record['hash']],'delete-local-data':False})
        with self.db() as conn:
            conn.execute('DELETE FROM cache WHERE key IN (?,?)',('prepared:'+key,'resolved:'+key))
        self.jf('/Library/Refresh',{},method='POST')
        self.log('Saved download ready: '+key)

    def cleanup_saved(self):
        if not any(r.get('state')=='ready' for r in self.records().values()):return
        sessions=self.jf('/Sessions')
        users=self.jf('/Users')
        # Enumerating every series WITH per-user state computes aggregates for
        # every episode. Read the slim native index, then only the exact watched
        # records needed for managed files via Jellyfin's dedicated UserData API.
        index=self.cached('saved-native-index')
        if index is None:
            index=[];start=0
            while True:
                params={'Recursive':'true','IncludeItemTypes':'Movie,Series','Fields':'Path,ProviderIds',
                        'EnableUserData':'false','Limit':1000,'StartIndex':start}
                result=self.jf('/Items?'+U.urlencode(params));batch=result.get('Items',[]);index+=batch
                start+=len(batch)
                if not batch or start>=result.get('TotalRecordCount',start):break
            self.remember('saved-native-index',index,300)
        for key,record in self.records().items():
            if record.get('state')!='ready' or self.verification_held(key):continue
            kind,imdb,*numbers=key.split(':')
            relevant=[];played=False;current_watch={}
            for user in users:
                items=[dict(i) for i in index if i.get('ProviderIds',{}).get('Imdb')==imdb
                       and i.get('Type')==('Movie' if kind=='movie' else 'Series')]
                if numbers:
                    episodes=[]
                    for series in items:
                        episodes+=self.jf('/Shows/'+series['Id']+'/Episodes?'+U.urlencode(
                            {'UserId':user['Id'],'Season':numbers[0],'Fields':'Path'})).get('Items',[])
                    items=[e for e in episodes if e.get('IndexNumber')==int(numbers[1])]
                relevant+=items
                for item in items:
                    data=self.jf('/UserItems/'+item['Id']+'/UserData?'+U.urlencode({'UserId':user['Id']}))
                    watchkey=user['Id']+':'+item['Id']
                    current_watch[watchkey]=bool(data.get('Played'))
                    recent=False
                    try:recent=dt.datetime.fromisoformat(data['LastPlayedDate'].replace('Z','+00:00')).timestamp()>=record['requested_at']
                    except (KeyError,ValueError,TypeError):pass
                    transitioned=watchkey in record.get('watch_baseline',{}) and not record['watch_baseline'][watchkey] and bool(data.get('Played'))
                    played=played or bool(data.get('Played')) and (recent or transitioned)
            ids={i['Id'] for i in relevant}
            if any(s.get('NowPlayingItem',{}).get('Id') in ids for s in sessions):continue
            expired=time.time()-record['completed_at']>=30*86400
            if not played and not expired:
                record['watch_baseline']=current_watch;self.save_record(key,record);continue
            path=Path(record['path'])
            if not any(path.resolve().is_relative_to(self.media/folder/'Saved') for folder in ('movies','shows')):
                raise ValueError('Refusing cleanup outside managed saved folders')
            if path.is_file() and (path.stat().st_size!=record['size'] or path.stat().st_ino!=record['inode']
                                  or record.get('mtime_ns',path.stat().st_mtime_ns)!=path.stat().st_mtime_ns):
                self.log('Saved file changed; cleanup skipped for '+key);continue
            for value in [str(path),*record.get('sidecars',[])]:
                file=Path(value)
                if file.parent!=path.parent or file.is_symlink():continue
                file.unlink(missing_ok=True)
            record.update(state='removed',removed_at=time.time());self.save_record(key,record)
            self.validation_result(key,False,reason='Saved copy removed; online source needs verification')
            with self.db() as conn:conn.execute('DELETE FROM cache WHERE key=?',('prepared:'+key,))
            self.jf('/Library/Refresh',{},method='POST')
            self.log('Saved download removed after watching/retention: '+key)
