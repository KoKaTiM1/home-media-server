#!/usr/bin/python3
"""Read-only OMV worker telemetry. Never exposes source URLs or credentials."""
import argparse
from contextlib import closing
from datetime import datetime
import json
import os
from pathlib import Path
import re
import sqlite3
import subprocess
import time

DATA=Path('/srv/dev-disk-by-uuid-bd267f22-f18a-4966-94ae-409a27252905/appdata/catalogue')
JOBS=[
 ('media-source-availability','Stream verification','Repeated batches; 1 minute between batches'),
 ('media-remote-availability','Windows LAN source verification','Optional parallel worker; 15 seconds between batches'),
 ('media-verification-coordinator','LAN media verification gateway','Continuous; sends exact-source media samples to the Windows worker'),
 ('media-verification-guard','Windows verification memory gate','Every 15 seconds; protects server memory'),
 ('media-catalogue-sync','New-title discovery','00:00, 06:00, 12:00, 18:00; up to 2 minutes jitter'),
 ('media-saved-downloads','Saved downloads and watched-file cleanup','Every minute'),
 ('media-catalogue-badges','Hebrew / English subtitle badges','Hourly'),
 ('media-catalogue-collections','Jellyfin collections','Every 30 minutes'),
 ('media-cleanup','Download retention cleanup','Hourly'),
 ('media-worker-status','Worker status counters','1 minute after previous snapshot'),
]
PROPS='Id,ActiveState,SubState,Result,ExecMainStatus,ExecMainStartTimestamp,ExecMainExitTimestamp,InactiveEnterTimestamp,LastTriggerUSec,NextElapseUSecRealtime,NextElapseUSecMonotonic,UnitFileState'

def stamp(epoch):
    return datetime.fromtimestamp(epoch).astimezone().strftime('%Y-%m-%d %H:%M:%S %Z') if epoch else 'Never'

def span(seconds):
    seconds=max(0,int(seconds))
    if seconds<60:return f'{seconds}s'
    if seconds<3600:return f'{seconds//60}m {seconds%60}s'
    return f'{seconds//3600}h {(seconds%3600)//60}m'

def systemd():
    units=[base+suffix for base,_,_ in JOBS for suffix in (('.service',) if base=='media-verification-coordinator' else ('.service','.timer'))]
    run=subprocess.run(['systemctl','show',*units,'--property='+PROPS],capture_output=True,text=True,timeout=8,check=True)
    result={}
    for block in run.stdout.strip().split('\n\n'):
        props=dict(line.split('=',1) for line in block.splitlines() if '=' in line)
        if props.get('Id'):result[props['Id']]=props
    return result

def context(data=DATA,cache_key="validation-worker"):
    try:
        with closing(sqlite3.connect('file:'+str(data/'catalogue.sqlite')+'?mode=ro',uri=True,timeout=2)) as conn:
            row=conn.execute("SELECT value FROM cache WHERE key=? AND expires>?",(cache_key,time.time())).fetchone()
            value=json.loads(row[0]) if row else {}
            if value.get('key') and conn.execute("SELECT 1 FROM sqlite_master WHERE name='preparation_work'").fetchone():
                work=conn.execute('SELECT value,updated FROM preparation_work WHERE key=?',(value['key'],)).fetchone()
                if work and time.time()-work[1]<600:value['preparation']=json.loads(work[0])
        if value.get('key'):
            tokens=value['key'].split(':')
            try:
                with closing(sqlite3.connect('file:'+str(data/'imdb/catalogue.sqlite')+'?mode=ro',uri=True,timeout=2)) as bulk:
                    title=bulk.execute('SELECT name FROM titles WHERE imdb=?',(tokens[1],)).fetchone()
                if title:value['name']=title[0]+(f' S{tokens[2]}E{tokens[3]}' if len(tokens)==4 else '')
            except sqlite3.Error:pass
        return value
    except (sqlite3.Error,OSError):return {}

def workers():
    units=systemd();current=context();rows=[]
    for base,name,schedule in JOBS:
        service=units.get(base+'.service',{});timer=units.get(base+'.timer',{})
        running=service.get('ActiveState') in ('active','activating','deactivating')
        failed=service.get('ActiveState')=='failed' or service.get('Result','success')!='success'
        continuous=base=='media-verification-coordinator'
        enabled=(service.get('UnitFileState')=='enabled' and running) if continuous else (timer.get('UnitFileState')=='enabled' and timer.get('ActiveState')=='active')
        if failed:health='🔴 Failed'
        elif not enabled:health='🟠 Service disabled / stopped' if continuous else '🟠 Timer disabled / stopped'
        elif running:health='🔵 Running'
        else:health='🟢 Healthy — waiting'
        detail=''
        if base=='media-source-availability':
            if running and current.get('key'):
                detail=f"Checking {current.get('name',current['key'])} · {span(time.time()-current.get('started',time.time()))} elapsed · batch {current.get('position',0)}"
                work=current.get('preparation',{})
                if work:
                    detail+=' · '+work.get('phase',work.get('state','preparing'))
                    if 'received_bytes' in work:
                        detail+=f" · {work['received_bytes']:,} useful bytes · {work.get('speed',0):,} B/s · {work.get('buffer_bytes',0):,} buffered"
                    if work.get('last_useful_progress'):
                        detail+=' · last progress '+span(time.time()-work['last_useful_progress'])+' ago'
                    if work.get('audio'):detail+=' · '+work['audio']
            else:detail=current.get('message','Progress is saved on disk; initial and daily audits run automatically')
        next_run=timer.get('NextElapseUSecRealtime') or ''
        countdown=''
        if next_run:
            # systemd prints a local wall-clock date; preserve its zone label.
            try:
                date=' '.join(next_run.split()[1:3])
                target=datetime.strptime(date,'%Y-%m-%d %H:%M:%S')
                countdown=span((target-datetime.now()).total_seconds())
            except ValueError:pass
        else:
            mono=timer.get('NextElapseUSecMonotonic','')
            units_s={'d':86400,'h':3600,'min':60,'s':1,'ms':.001,'us':.000001}
            parts=re.findall(r'([\d.]+)(min|ms|us|d|h|s)',mono)
            if parts:
                remaining=sum(float(n)*units_s[u] for n,u in parts)-time.monotonic()
                next_run=stamp(time.time()+max(0,remaining));countdown=span(remaining)
            elif running:next_run='After current batch / run';countdown='Running now'
            else:next_run='Waiting for scheduler'
        if base=='media-remote-availability':
            remote=context(cache_key='remote-validation-worker')
            detail=remote.get('message','Waiting for the enrolled Windows PC and resource gate')
            if running and remote.get('key'):
                phase=remote.get('preparation',{}).get('phase',detail)
                detail='Windows: '+remote.get('name',remote['key'])+' · '+('Title details' if phase=='metadata' else phase)
                if remote.get('batch'):detail+=f" · run {remote['batch']} item {remote.get('position',0)}/20"
        if continuous:
            next_run='Continuous service';countdown='Running now' if running else 'Stopped'
            detail='Windows decodes exact selected-source samples; server validates and commits proofs'
        rows.append({'id':base,'name':name,'health':health,'schedule':schedule,
            'last_run':service.get('ExecMainStartTimestamp') or timer.get('LastTriggerUSec') or 'Never',
            'last_result':'In progress' if running else service.get('Result','Not run'),
            'next_run':next_run,'countdown':countdown,'detail':detail})
    return rows

def foreground_busy(data=DATA):
    try:return float((data.parent.parent/'catalogue/.foreground-read-until').read_text())>time.time()
    except (OSError,ValueError):return False

def counts(data=DATA,refresh=False):
    if foreground_busy(data):
        try:return json.loads(Path('/run/media-worker-counts.json').read_text())['rows']
        except (OSError,ValueError,KeyError):return []
    now=time.time();cache=Path('/run/media-worker-counts.json')
    if data==DATA and cache.exists():
        try:
            saved=json.loads(cache.read_text())
            if not refresh or now-saved['time']<60:return saved['rows']
        except (OSError,ValueError,KeyError):pass
    if data==DATA and not refresh:
        return [{'id':'initializing','name':'Counter snapshot','value':'Initializing','detail':'Background counter job is preparing the first snapshot','updated':'Waiting for first snapshot'}]
    rows=[];summary={}
    def add(name,value,detail=''):rows.append({'id':str(len(rows)),'name':name,'value':f'{value:,}' if isinstance(value,int) else str(value),'detail':detail,'updated':stamp(now)})
    with closing(sqlite3.connect('file:'+str(data/'catalogue.sqlite')+'?mode=ro',uri=True,timeout=3)) as conn:
        conn.set_progress_handler(lambda:int(foreground_busy(data)),10000)
        grouped=dict(conn.execute('SELECT state,COUNT(*) FROM validation GROUP BY state'))
        summary.update(total=sum(grouped.values()),pending=grouped.get('pending',0),
                       unavailable=grouped.get('unavailable',0),playable=grouped.get('verified',0),v5=0,
                       audit_done=0,audit_total=0)
        add('Waiting for verification / recheck',grouped.get('pending',0),'Movie, first-episode and additional-episode source keys')
        add('Failed / not passed — retained for retry',grouped.get('unavailable',0),'Not deleted; retry waits increase up to 30 days')
        due=conn.execute("SELECT COUNT(*) FROM validation WHERE state='unavailable' AND next_check<=?",(now,)).fetchone()[0]
        add('Failed checks due for retry now',due,'One in five probes is reserved for due failures')
        add('Passed source / episode checks',grouped.get('verified',0),'Several episode checks can belong to one visible show')
        converted=conn.execute("SELECT COUNT(*) FROM validation WHERE state='verified' AND json_extract(proof,'$.audio_policy')=2 AND json_extract(proof,'$.audio_conversion')=1").fetchone()[0]
        add('Passed sources using verified AAC-LC conversion',converted,'Video stays unchanged; sources with direct AAC-LC do not need conversion')
        if conn.execute("SELECT 1 FROM sqlite_master WHERE name='verification_audit'").fetchone():
            audit=dict(conn.execute("SELECT CASE WHEN completed=0 THEN 'pending' ELSE result END,COUNT(*) FROM verification_audit GROUP BY 1"))
            summary.update(audit_done=sum(n for state,n in audit.items() if state!='pending'),audit_total=sum(audit.values()))
            if audit.get('pending',0):
                for status,label in [('pending','awaiting check'),('passed','passed'),('failed','failed')]:
                    add('Initial stronger-verification audit — '+label,audit.get(status,0),'Existing titles remain visible until a fresh failure; reports run first')
            v5=conn.execute("SELECT COUNT(*),SUM(CASE WHEN json_extract(proof,'$.audio_conversion')=1 THEN 1 ELSE 0 END),SUM(CASE WHEN checked>? THEN 1 ELSE 0 END) FROM validation WHERE state='verified' AND json_extract(proof,'$.version')=5",(now-3600,)).fetchone()
            summary.update(v5=v5[0],v5_hour=v5[2] or 0)
            add('Passed stronger video/audio checks',v5[0],'Decoded beginning and later samples from the exact selected torrent/file')
            add('Stronger checks — direct audio',v5[0]-(v5[1] or 0))
            add('Stronger checks — AAC-LC conversion',v5[1] or 0,'Video copied; conversion samples actually decoded')
            due_audits=conn.execute("SELECT COUNT(*) FROM validation WHERE state='verified' AND checked<=?",(now-86400,)).fetchone()[0]
            add('Successful sources due for daily audit',due_audits,'One in five ordinary slots after the initial audit')
            preparing=conn.execute('SELECT COUNT(*) FROM preparation_work').fetchone()[0]
            add('Preparation checkpoints waiting to resume',preparing,'Progress is saved across request limits, viewing pauses and reboot')
        elif conn.execute("SELECT 1 FROM sqlite_master WHERE name='audio_recheck'").fetchone():
            audits=conn.execute('SELECT COUNT(*) FROM audio_recheck WHERE completed=0').fetchone()[0]
            add('Passed sources awaiting new audio-policy audit',audits,'Audited gradually without clearing the verified catalogue')
        marker=conn.execute("SELECT value FROM cache WHERE key IN ('audio-policy-resync-v1','audio-policy-resync-v2') ORDER BY key DESC LIMIT 1").fetchone()
        if marker:
            migration=json.loads(marker[0])
            if isinstance(migration,dict):
                add('Previously failed entries scheduled for audio resync',migration.get('failed_scheduled',0),'One-time resync; later failures keep normal increasing retry waits')
                add('Audio resync scheduled at',stamp(migration.get('scheduled_at',0)))

        if conn.execute("SELECT 1 FROM sqlite_master WHERE name='manual_rechecks'").fetchone():
            reports=dict(conn.execute('SELECT status,COUNT(*) FROM manual_rechecks GROUP BY status'))
            add('User reports awaiting fresh check',reports.get('pending',0),'Prioritized ahead of discovery; waits while playback or navigation is active')
            add('User reports failed latest check',reports.get('failed',0),'Hidden from ordinary browsing; retained for scheduled retries')
            add('User reports passed fresh check',reports.get('passed',0),'Fresh source check passed after the latest report')
        if conn.execute("SELECT 1 FROM sqlite_master WHERE name='verification_holds'").fetchone():
            held=conn.execute('SELECT COUNT(*),MIN(expires) FROM verification_holds WHERE expires>?',(now,)).fetchone()
            add('Titles protected from verification / cleanup',held[0],('Earliest hold ends '+stamp(held[1]) if held[1] else 'No active holds')+'; other titles continue normally')
        pending=dict(conn.execute("SELECT CASE WHEN key LIKE 'movie:%' THEN 'Movies' WHEN key LIKE '%:1:1' THEN 'Shows' ELSE 'Episodes' END,COUNT(*) FROM validation WHERE state='pending' GROUP BY 1"))
        for kind in ('Movies','Shows','Episodes'):add(kind+' waiting for check',pending.get(kind,0),'Shows use S1E1 as the initial check; other episodes counted separately')
        distinct=conn.execute("SELECT COUNT(DISTINCT CASE WHEN key LIKE 'movie:%' THEN key END),COUNT(DISTINCT CASE WHEN key LIKE 'series:%' THEN substr(key,1,instr(substr(key,8),':')+6) END) FROM validation WHERE state='verified'").fetchone()
        add('Passed distinct movies',distinct[0]);add('Passed distinct shows',distinct[1])
        recent=conn.execute('SELECT COUNT(*),MAX(checked) FROM validation WHERE checked>?',(now-3600,)).fetchone()
        add('Keys with a completed check in the last hour',recent[0],'Latest result per key; includes failures, not a count of new visible titles')
        latest=conn.execute('SELECT MAX(checked) FROM validation').fetchone()[0]
        add('Most recent completed check',stamp(latest))
        sample=data.parent.parent/'media/movies/Tests/AAC Main TV test/AAC Main TV test.mp4'
        status='Ready for TV test; confirmation pending' if sample.is_file() else 'Test sample not available'
        try:status=json.loads((data/'aac-main-tv-result.json').read_text()).get('status',status)
        except (OSError,ValueError,TypeError):pass
        add('AAC Main TV test (Big Buck Bunny)',status,'AAC-LC reference is in local Movies; reported TV results are saved separately from source validation')
        conn.execute('ATTACH DATABASE ? AS bulk',('file:'+str(data/'imdb/catalogue.sqlite')+'?mode=ro',))
        index=data/'imdb/catalogue.sqlite';identity=[index.stat().st_ino,index.stat().st_mtime_ns]
        metadata_file=Path('/run/media-worker-index-counts.json');metadata=None
        if data==DATA:
            try:
                previous=json.loads(metadata_file.read_text())
                if previous['identity']==identity and now-previous['time']<300:metadata=previous
            except (OSError,ValueError,KeyError):pass
        if metadata is None:
            total=conn.execute('SELECT COUNT(*) FROM bulk.titles').fetchone()[0]
            conn.execute('PRAGMA temp_store=MEMORY')
            outside=conn.execute("SELECT COUNT(*) FROM (SELECT kind||':'||imdb||CASE WHEN kind='series' THEN ':1:1' ELSE '' END FROM bulk.titles EXCEPT SELECT key FROM validation)").fetchone()[0]
            metadata={'identity':identity,'time':now,'total':total,'outside':outside}
            if data==DATA:
                temporary=metadata_file.with_name(metadata_file.name+'.'+str(os.getpid()))
                temporary.write_text(json.dumps(metadata));temporary.replace(metadata_file)
        add('Metadata titles indexed locally',metadata['total'],'Discovery metadata is not proof of playable video')
        rows[-1]['updated']=stamp(metadata['time'])
        add('Indexed titles outside the queue',metadata['outside'],'Normally zero; metadata counters cache up to 5 minutes')
        rows[-1]['updated']=stamp(metadata['time'])
        for kind in ('movie','series'):
            identity="substr(v.key,7)" if kind=='movie' else "substr(v.key,8,instr(substr(v.key,8),':')-1)"
            query="SELECT COUNT(DISTINCT t.imdb) FROM validation v CROSS JOIN bulk.titles t ON t.imdb="+identity+" WHERE v.state='verified' AND t.kind=?"
            query+=" AND NOT EXISTS(SELECT 1 FROM cache c WHERE c.key='source-status:'||v.key AND c.expires>strftime('%s','now') AND json_extract(c.value,'$.state')='unavailable' AND COALESCE(json_extract(c.value,'$.checked_at'),0)>v.checked)"
            columns={r[1] for r in conn.execute('PRAGMA bulk.table_info(titles)')}
            dates=conn.execute("SELECT 1 FROM sqlite_master WHERE name='release_dates'").fetchone()
            if dates:
                query+=" AND NOT EXISTS(SELECT 1 FROM release_dates d WHERE d.kind=t.kind AND d.imdb=t.imdb AND d.released>date('now','localtime'))"
                if {'year','votes'}<=columns:
                    query+=" AND (t.year<CAST(strftime('%Y','now','localtime') AS INTEGER) OR t.votes>0 OR EXISTS(SELECT 1 FROM release_dates d WHERE d.kind=t.kind AND d.imdb=t.imdb AND d.released<=date('now','localtime')))"
            visible=conn.execute(query,(kind,)).fetchone()[0]
            summary['ready_'+('movies' if kind=='movie' else 'shows')]=visible
            add('Verified indexed '+('movies' if kind=='movie' else 'shows'),visible,'Excludes known future releases and newer failed checks; saved-only media excluded')
    verification=data.parent/'verification-worker/queue.sqlite'
    if verification.is_file():
        try:
            with closing(sqlite3.connect(verification.as_uri()+'?mode=ro',uri=True,timeout=1)) as remote:
                totals=dict(remote.execute('SELECT key,value FROM totals'))
                controls={k:json.loads(v) for k,v in remote.execute('SELECT key,value FROM control')}
                seen=[row[0] for row in remote.execute('SELECT seen FROM client')]
                if remote.execute("SELECT 1 FROM sqlite_master WHERE name='second_client'").fetchone():
                    seen += [row[0] for row in remote.execute('SELECT seen FROM second_client')]
                inflight=remote.execute("SELECT COUNT(*) FROM decode_jobs WHERE state IN ('pending','leased') AND controller>?",(now,)).fetchone()[0]
            add('Windows source checks passed',totals.get('windows_titles_verified',0),'Actual v5 verification runs; distinct from metadata uploads')
            add('Windows source checks unavailable',totals.get('windows_titles_unavailable',0),'Failures retained under the normal retry policy')
            add('Windows source checks checkpointed',totals.get('windows_title_checkpoints',0),'Interrupted work is not a failed source')
            add('Windows active decoding operations',inflight,'Probe or beginning/later sample; bounded by the resource gate')
            add('Windows verification last seen',stamp(max(seen,default=0)),'Separate from the metadata helper')
            add('Windows verification gate',controls.get('reason','Not enabled'),'One server plus one Windows verifier only after memory approval')
        except (sqlite3.Error,OSError,ValueError):pass
    c=context(data)
    if c.get('message'):add('Validator activity',c.get('name') or c.get('key') or c['message'],c['message'])
    if data==DATA:
        import tempfile
        with tempfile.NamedTemporaryFile(mode='w',dir='/run',prefix='media-worker-counts-',delete=False) as output:
            json.dump({'time':now,'rows':rows,'summary':summary},output);temporary=Path(output.name)
        temporary.replace(cache)
    return rows

def overview(data=DATA,snapshot=None):
    """Small cached summary plus live, scoped Windows operation state."""
    now=time.time();snapshot=snapshot or Path('/run/media-worker-counts.json')
    try:saved=json.loads(snapshot.read_text())
    except (OSError,ValueError):saved={}
    values=saved.get('summary') or {}
    updated=stamp(saved.get('time',0)) if values else 'Waiting for snapshot'
    rows=[]
    def add(name,value,detail,tone='muted'):
        rows.append({'id':str(len(rows)),'name':name,'value':str(value),'detail':detail,'tone':tone,'updated':updated})
    if not values:
        add('Verification overview','Preparing','The background counter snapshot is still running')
    else:
        total=max(0,values.get('total',0));verified=max(0,values.get('v5',0))
        add('Final v5 verified',f'{verified:,}',f'{100*verified/total:.2f}% of {total:,} source keys; full beginning and later sample proof' if total else 'Full beginning and later sample proof','success')
        if 'ready_movies' in values:add('Verified movies',f"{values['ready_movies']:,}",'Distinct indexed movies currently visible from passed source checks','success')
        if 'ready_shows' in values:add('Verified shows',f"{values['ready_shows']:,}",'Distinct indexed shows with at least one passed episode','success')
        add('Not yet v5 verified',f'{max(0,total-verified):,}',f"Includes {values.get('pending',0):,} awaiting a check, retries and older proof versions")
        add('Unavailable / retry',f"{values.get('unavailable',0):,}",'Failed outcomes are retained and retried on schedule','error' if values.get('unavailable') else 'muted')
        audit_total=values.get('audit_total',0);audit_done=values.get('audit_done',0)
        if audit_total and audit_done<audit_total:
            add('Initial v5 audit',f'{audit_done:,} / {audit_total:,}',f'{100*audit_done/audit_total:.1f}% complete')
        add('Final passes in last hour',f"{values.get('v5_hour',0):,}",'Latest verified state per title; not a count of decoder operations','success')
    updated=stamp(now)
    queue=data.parent/'verification-worker/queue.sqlite'
    try:
        with closing(sqlite3.connect('file:'+str(queue)+'?mode=ro',uri=True,timeout=1)) as conn:
            conn.row_factory=sqlite3.Row
            totals=dict(conn.execute('SELECT key,value FROM totals'))
            control={key:json.loads(value) for key,value in conn.execute("SELECT key,value FROM control WHERE key IN ('enabled','reason','max_windows_jobs')")}
            seen=[]
            # Count every separately enrolled Windows lane. Older dashboard
            # code only read the original two tables and reported 2/2 even
            # after lanes three and four had connected.
            for table in ('client','second_client','third_client','fourth_client'):
                if conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",(table,)).fetchone():
                    seen += [row[0] for row in conn.execute('SELECT seen FROM '+table)]
            active=conn.execute("SELECT key,operation,params,state,created,stream_started,phase,phase_at FROM decode_jobs WHERE state IN ('pending','leased','result') AND controller>? ORDER BY created LIMIT 1",(now,)).fetchone()
            problem=conn.execute("SELECT operation,outcome,reason_code,finished FROM decode_jobs WHERE outcome IN ('checkpoint','decode_failure') AND finished>? ORDER BY finished DESC LIMIT 1",(now-3600,)).fetchone()
        add('Windows final passes',f"{totals.get('windows_titles_verified',0):,}",'Completed full title checks on the PC; probes and samples excluded','success')
        online=sum(value>now-45 for value in seen)
        connected=online>0
        add('Windows worker','Connected' if connected else 'Offline',control.get('reason','Waiting for resource gate') if connected else 'Server verification continues without the PC','success' if connected and control.get('enabled') else 'muted')
        capacity=max(1,min(4,int(control.get('max_windows_jobs',1))))
        add('Windows lanes',f'{online} / {capacity}','Connected verifier containers / allowed simultaneous Windows operations','success' if online and control.get('enabled') else 'muted')
        if active:
            key=active['key'];details=context(data,cache_key='remote-validation-worker')
            title=details.get('name') if details.get('key')==key else None
            if not title:title='Movie' if key.startswith('movie:') else 'Episode'
            params=json.loads(active['params']) if active['params'] else {}
            phase=active['phase'] or ''
            if active['state']=='pending':stage='Queued for Windows'
            elif active['state']=='result':stage='Result received; server checking'
            elif phase=='video probe':stage='Probing video/audio'
            elif phase=='audio conversion':stage='Converting audio sample'
            elif phase=='conversion probe':stage='Checking converted audio'
            elif phase=='sample decode':stage='Decoding '+('later' if params.get('position',0)>0 else 'beginning')+' sample'
            elif active['operation']=='probe':stage='Fetching source for video probe' if active['stream_started'] else 'Starting video probe'
            else:stage='Fetching source for '+('later' if params.get('position',0)>0 else 'beginning')+' sample' if active['stream_started'] else 'Starting sample'
            progress=details.get('preparation',{}) if details.get('key')==key else {}
            extra=f" · {progress['received_bytes']/1048576:.1f} MiB received" if isinstance(progress.get('received_bytes'),(int,float)) else ''
            add('Windows working on',title,stage+' · '+span(now-active['created'])+' elapsed'+extra,'success')
            position=details.get('position',0) if details.get('key')==key else 0
            batch=details.get('batch',0) if details.get('key')==key else 0
            if position:add('Windows batch',f"Run {batch or '?'} · item {position}/20",'A batch ends after at most 20 title attempts or eight minutes')
        else:add('Windows working on','Waiting','The next guarded batch starts automatically when work is eligible')
        if problem:
            label='Decode rejected' if problem['outcome']=='decode_failure' else 'Checkpoint'
            reason=problem['reason_code'] or 'reason unavailable from current PC client'
            add('Latest PC operation issue',label,f"{problem['operation']} · {reason} · {span(now-problem['finished'])} ago",'error')
        else:add('Latest PC operation issue','None in last hour','Older issues remain in the detailed worker log')
    except (sqlite3.Error,OSError,ValueError,TypeError):
        add('Windows worker','Status unavailable','Live operation state will return when the coordinator is readable')
    return rows

if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('section',choices=('workers','counts','snapshot','overview'));args=parser.parse_args()
    try:rows=workers() if args.section=='workers' else overview() if args.section=='overview' else counts(refresh=args.section=='snapshot')
    except sqlite3.OperationalError:
        if args.section=='snapshot' and foreground_busy():
            print('Counter snapshot yielded to navigation; previous snapshot retained')
            raise SystemExit(0)
        raise
    print('Worker counter snapshot updated: '+str(len(rows))+' metrics' if args.section=='snapshot' else json.dumps(rows,ensure_ascii=False))
