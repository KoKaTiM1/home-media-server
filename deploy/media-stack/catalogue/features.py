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
import urllib.error
import urllib.parse as U
import urllib.request as R


class Features:
    def __init__(self, runtime):
        self.g=runtime
        self.download_lock=threading.Lock()
        self.count_lock=threading.Lock()
        self.count_cache={}

    def db(self):
        conn=self.g['db']()
        conn.execute('CREATE TABLE IF NOT EXISTS discovery (kind TEXT, imdb TEXT, meta TEXT, PRIMARY KEY(kind,imdb))')
        conn.execute('CREATE TABLE IF NOT EXISTS release_dates (kind TEXT, imdb TEXT, released TEXT, PRIMARY KEY(kind,imdb))')
        conn.execute('CREATE TABLE IF NOT EXISTS aliases (imdb TEXT PRIMARY KEY, hebrew TEXT, english TEXT)')
        conn.execute('CREATE TABLE IF NOT EXISTS downloads (key TEXT PRIMARY KEY, value TEXT)')
        conn.execute("CREATE TABLE IF NOT EXISTS validation (key TEXT PRIMARY KEY, state TEXT NOT NULL DEFAULT 'pending', attempts INTEGER NOT NULL DEFAULT 0, next_check REAL NOT NULL DEFAULT 0, checked REAL NOT NULL DEFAULT 0, proof TEXT, reason TEXT, priority INTEGER NOT NULL DEFAULT 0)")
        conn.execute('CREATE TABLE IF NOT EXISTS audio_recheck (key TEXT PRIMARY KEY, completed REAL NOT NULL DEFAULT 0)')
        conn.execute('CREATE INDEX IF NOT EXISTS validation_due ON validation(state,next_check,priority)')
        conn.execute("CREATE INDEX IF NOT EXISTS validation_work ON validation(priority DESC,next_check) WHERE state!='verified'")
        return conn

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
        with self.db() as conn:row=conn.execute('SELECT hebrew,english FROM aliases WHERE imdb=?',(imdb,)).fetchone()
        if row and row[0] and re.search('[\u0590-\u05ff]',row[0]):
            english=row[1] or meta.get('name','')
            result['originalName']=english
            result['name']=row[0]+(' / '+english if english and english!=row[0] else '')
        return result

    def poster_url(self,kind,imdb,badge=None):
        if badge is None:badge=self.g['poster_status'](kind,imdb)
        return f"{self.g['BASE']}/poster/{kind}/{imdb}.jpg?style={self.g.get('POSTER_STYLE','3')}&badge={badge}"

    def decorate_posters(self,metas):
        # Keep provider artwork in storage; expose only the local badged artwork.
        with self.db() as conn:
            for meta in metas:
                kind=meta.get('type');imdb=meta.get('id','')
                if kind not in ('movie','series') or not re.fullmatch(r'tt\d{5,12}',imdb):continue
                original=dict(meta)
                if not str(original.get('poster','')).startswith('https://'):
                    original['poster']=f'https://images.metahub.space/poster/medium/{imdb}/img'
                conn.execute('INSERT INTO discovery VALUES (?,?,?) ON CONFLICT(kind,imdb) DO UPDATE SET meta=excluded.meta WHERE discovery.meta!=excluded.meta',(kind,imdb,json.dumps(original)))
        badges=self.g['poster_badges'](metas)
        for meta in metas:
            kind=meta.get('type');imdb=meta.get('id','')
            if kind in ('movie','series') and re.fullmatch(r'tt\d{5,12}',imdb):
                badge=badges[(kind,imdb)]
                meta['poster']=self.poster_url(kind,imdb,badge)
        return metas

    def enqueue(self,key,priority=0):
        if not re.fullmatch(r'(movie:tt\d{5,12}|series:tt\d{5,12}:\d+:\d+)',key):return
        with self.db() as conn:
            conn.execute('INSERT INTO validation(key,priority) VALUES (?,?) ON CONFLICT(key) DO UPDATE SET priority=MAX(priority,excluded.priority)',(key,priority))

    def enqueue_many(self,keys,priority=0):
        valid={key for key in keys if re.fullmatch(r'(movie:tt\d{5,12}|series:tt\d{5,12}:\d+:\d+)',key)}
        with self.db() as conn:
            conn.executemany('INSERT INTO validation(key,priority) VALUES (?,?) ON CONFLICT(key) DO UPDATE SET priority=MAX(priority,excluded.priority)',((key,priority) for key in valid))

    def validation_result(self,key,ok,proof=None,reason=''):
        self.enqueue(key)
        now=time.time()
        with self.db() as conn:
            attempts=conn.execute('SELECT attempts FROM validation WHERE key=?',(key,)).fetchone()[0]
            attempts=0 if ok else attempts+1
            # Preserve successful proof even after failure, but never let it
            # override a newer failed check. Backoff: 6h, 12h, 1d ... max 30d.
            delay=min(30*86400,21600*2**min(max(0,attempts-1),7))
            conn.execute('UPDATE validation SET state=?,attempts=?,next_check=?,checked=?,proof=COALESCE(?,proof),reason=? WHERE key=?',
                ('verified' if ok else 'unavailable',attempts,0 if ok else now+delay,now,json.dumps(proof) if proof else None,reason[:160],key))
            conn.execute('UPDATE audio_recheck SET completed=? WHERE key=?',(now,key))
            if not ok:conn.execute("DELETE FROM cache WHERE key IN (?,?)",('prepared:'+key,'resolved:'+key))
        with self.count_lock:self.count_cache.clear()

    def source_state(self,key):
        if self.local_download(key):return 'downloaded'
        with self.db() as conn:row=conn.execute('SELECT state,checked FROM validation WHERE key=?',(key,)).fetchone()
        if row:
            latest=self.cached('source-status:'+key) or {}
            if latest.get('state')=='unavailable' and latest.get('checked_at',0)>row[1]:return 'unavailable'
            return row[0]
        return 'unknown'

    def ready_keys(self):
        with self.db() as conn:
            rows=conn.execute("SELECT v.key FROM validation v LEFT JOIN cache c ON c.key='source-status:'||v.key AND c.expires>? WHERE v.state='verified' AND (c.key IS NULL OR json_extract(c.value,'$.state')!='unavailable' OR COALESCE(json_extract(c.value,'$.checked_at'),0)<=v.checked)",(time.time(),)).fetchall()
            downloads=conn.execute('SELECT key FROM downloads').fetchall()
        ready={key for (key,) in rows}
        for (key,) in downloads:
            if self.local_download(key):ready.add(key)
        return ready

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
                conn.execute('CREATE TEMP TABLE ready_titles(identity TEXT PRIMARY KEY)')
                conn.executemany('INSERT INTO ready_titles VALUES (?)',((v,) for v in ready))
                where+=" AND (t.kind||':'||t.imdb) IN (SELECT identity FROM ready_titles)"
                unavailable=[row[0] for row in conn.execute("SELECT kind||':'||imdb FROM state.release_dates WHERE released>?",(dt.date.today().isoformat(),))]
                if unavailable:
                    where+=" AND (t.kind||':'||t.imdb) NOT IN ("+','.join('?' for _ in unavailable)+')'
                    params.extend(unavailable)
                indexes={row[1] for row in conn.execute('PRAGMA index_list(titles)')}
                count_source='titles t INDEXED BY browse_titles' if 'browse_titles' in indexes else 'titles t'
                if month_bounds and 'browse_release_lookup' in indexes:count_source='titles t INDEXED BY browse_release_lookup'
                identity=index.stat()
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
                preferred={'t.votes':'browse_titles','t.rating':'browse_score','t.name COLLATE NOCASE':'browse_name'}.get(expression)
                covering={'t.votes':'browse_titles','t.rating':'browse_score_cover','t.name COLLATE NOCASE':'browse_name_cover','t.runtime':'browse_runtime_cover'}.get(expression)
                if expression=='t.released':covering='browse_release_cover'
                if month_bounds:covering='browse_release_lookup'
                if covering in indexes:preferred=covering
                rank_source='titles t'+(' INDEXED BY '+preferred if preferred in indexes else '')
                ordering=f't.year DESC,{expression} {order} NULLS LAST,t.imdb'
                # Rank compact index entries first; fetch full rows/aliases only
                # for this page, rather than sorting thousands of poster previews.
                rows=conn.execute('WITH ranked AS (SELECT t.imdb FROM '+rank_source+' WHERE '+where+
                    ' ORDER BY '+ordering+' LIMIT ? OFFSET ?) SELECT t.imdb,t.name,t.original,t.year,t.genres,a.hebrew,'+
                    ('t.rating' if 'rating' in columns else 'NULL')+','+('t.runtime' if 'runtime' in columns else 'NULL')+
                    ',t.kind,'+('t.released' if 'released' in columns else 'NULL')+' FROM ranked r JOIN titles t ON t.imdb=r.imdb LEFT JOIN state.aliases a ON a.imdb=t.imdb ORDER BY '+ordering,
                    (*params,limit,offset)).fetchall()
            metas=[{'id':imdb,'type':actual_kind,'name':hebrew+' / '+name if hebrew else name,'originalName':original,
                'releaseInfo':str(release_year),'genres':genres.split(',') if genres else [],
                'poster':f'https://images.metahub.space/poster/medium/{imdb}/img',
                'released':released+'T00:00:00Z' if released else None,
                'imdbRating':str(rating) if rating is not None else None,'runtime':str(runtime)+' min' if runtime else None}
                for imdb,name,original,release_year,genres,hebrew,rating,runtime,actual_kind,released in rows]
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
        response=self.fetch('https://www.wikidata.org/w/api.php?'+U.urlencode(params),timeout=7)
        if response.get('error'):raise RuntimeError('Hebrew title provider temporarily busy')
        found=response.get('search',[])
        ids=[r['id'] for r in found if re.fullmatch(r'Q\d+',r.get('id',''))]
        if not ids:return self.remember(key,[],3600)
        params={'action':'wbgetentities','ids':'|'.join(ids),'props':'claims|labels',
                'languages':'he|en','format':'json','maxlag':30}
        response=self.fetch('https://www.wikidata.org/w/api.php?'+U.urlencode(params),timeout=7)
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
                    raw=self.fetch(f"{self.g['CINEMETA']}/catalog/{kind}/top/search={U.quote(query,safe='')}.json",timeout=8).get('metas',[])
                    self.remember(search_cache,raw,21600)
                except Exception:
                    raw=[];self.remember(search_cache,raw,300)
        local=self.index_search(kind,query);seen=set();output=[]
        if re.search('[\u0590-\u05ff]',query):
            for keyword in (self.cached('hebrew-keywords:'+self.search_key(query)) or [])[:5]:
                local+=self.index_search(kind,keyword)
        self.decorate_posters(raw+local)
        self.enqueue_many((kind+':'+meta.get('id','')+(':1:1' if kind=='series' else '') for meta in raw+local),100)
        ready=self.ready_titles()
        for meta in raw+local:
            imdb=meta.get('id')
            if not imdb or imdb in seen:continue
            if (meta.get('released') or '')[:10]>dt.date.today().isoformat():continue
            if kind+':'+imdb not in ready:continue
            seen.add(imdb);output.append(self.localize(meta))
        return self.decorate_posters(output)

    def warm_availability(self,limit=20):
        # flock survives separate cron/docker exec processes. Interrupted rows
        # remain due and resume automatically; no in-memory-only work cursor.
        with (self.g['DATA']/'validation.lock').open('a') as lock:
            try:fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
            except BlockingIOError:return
            # Defer all torrent probes while the owner is playing anything.
            def busy():
                try:return 'Paused while Jellyfin is playing' if any(s.get('NowPlayingItem') for s in self.jf('/Sessions')) else ''
                except Exception:return 'Waiting for Jellyfin startup or session status'
            blocked=busy()
            if blocked:
                self.remember('validation-worker',{'message':blocked+'; resumes automatically','started':time.time()},180)
                return
            if self.cached('validation-provider-backoff'):
                self.remember('validation-worker',{'message':'Source provider cooldown; resumes automatically','started':time.time()},180)
                return
            self.seed_validation()
            self.schedule_audio_resync()
            started=time.monotonic();checked=0
            while checked<limit and time.monotonic()-started<480:
                blocked=busy()
                if blocked:
                    self.remember('validation-worker',{'message':blocked+'; resumes automatically','started':time.time()},180)
                    break
                with self.db() as conn:
                    row=None
                    # Audit saved successful sources without resetting the
                    # visible catalogue. One in ten slots until completed.
                    if checked%10==9:
                        row=conn.execute("SELECT a.key FROM audio_recheck a JOIN validation v ON v.key=a.key WHERE a.completed=0 AND v.state='verified' ORDER BY a.rowid LIMIT 1").fetchone()
                    # Reserve one in five probes for due failures so a large
                    # discovery backlog cannot starve the saved retry list.
                    if checked%5==0:
                        row=conn.execute("SELECT key FROM validation WHERE state='unavailable' AND next_check<=? ORDER BY next_check,priority DESC LIMIT 1",(time.time(),)).fetchone()
                    if not row:
                        # Most slots widen the catalogue; one in five grows
                        # episode coverage of shows already visible.
                        breadth="(key LIKE 'movie:%' OR key LIKE 'series:%:1:1')"
                        wanted="NOT "+breadth if checked%5==4 else breadth
                        row=conn.execute("SELECT key FROM validation WHERE state!='verified' AND next_check<=? AND "+wanted+" ORDER BY priority DESC,next_check,rowid LIMIT 1",(time.time(),)).fetchone()
                    if not row:
                        row=conn.execute("SELECT key FROM validation WHERE state!='verified' AND next_check<=? ORDER BY priority DESC,next_check,rowid LIMIT 1",(time.time(),)).fetchone()
                if not row:break
                key=row[0]
                self.remember('validation-worker',{'key':key,'started':time.time(),'position':checked+1,'message':'Video sample validation running'},180)
                try:
                    # Retry only when due, with fresh source listings. A cached
                    # negative listing must not poison its own next retry.
                    with self.db() as conn:
                        conn.execute('DELETE FROM cache WHERE key IN (?,?,?)',('streams:'+key,'prepared:'+key,'resolved:'+key))
                    self.g['prepared'](key,background=True)
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
                    if isinstance(error,urllib.error.HTTPError) and (error.code==429 or error.code>=500):
                        self.remember('validation-provider-backoff',True,900 if error.code==429 else 300)
                        self.log('Validation paused for source provider cooldown')
                        break
                    with self.db() as conn:current=conn.execute('SELECT checked FROM validation WHERE key=?',(key,)).fetchone()
                    if not current or current[0]<time.time()-30:self.validation_result(key,False,reason=type(error).__name__)
                checked+=1
                self.remember('validation-worker',{'key':key,'started':time.time(),'position':checked,'message':'Last probe finished; proceeding to next queue entry'},180)
                time.sleep(2)
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
        with closing(sqlite3.connect(self.g['DATA']/'imdb/catalogue.sqlite')) as index:
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
                with self.db() as conn:conn.execute('DELETE FROM cache WHERE key=?',('meta:series:'+imdb,))
                meta=self.g['metadata']('series',imdb)
                old=json.loads(raw)
                if meta.get('videos')!=old.get('videos'):
                    self.g['import_item']('series',meta,'Opened');changed+=1
                time.sleep(.15)
            except Exception:self.log('Episode refresh deferred for '+imdb)
        if changed:self.jf('/Library/Refresh',{},method='POST')
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
            if record.get('state')!='ready':continue
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
