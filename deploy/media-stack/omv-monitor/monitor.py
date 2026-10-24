#!/usr/bin/python3
"""Read-only OMV worker telemetry. Never exposes source URLs or credentials."""
import argparse
from contextlib import closing
from datetime import datetime
import json
from pathlib import Path
import re
import sqlite3
import subprocess
import time

DATA=Path('/srv/dev-disk-by-uuid-<MEDIA_FILESYSTEM_UUID>/appdata/catalogue')
JOBS=[
 ('media-source-availability','Stream verification','Repeated batches; 1 minute between batches'),
 ('media-catalogue-sync','New-title discovery','00:00, 06:00, 12:00, 18:00; up to 2 minutes jitter'),
 ('media-saved-downloads','Saved downloads and watched-file cleanup','Every minute'),
 ('media-catalogue-badges','Hebrew / English subtitle badges','Hourly'),
 ('media-catalogue-collections','Jellyfin collections','Every 30 minutes'),
 ('media-cleanup','Download retention cleanup','Hourly'),
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
    units=[base+suffix for base,_,_ in JOBS for suffix in ('.service','.timer')]
    run=subprocess.run(['systemctl','show',*units,'--property='+PROPS],capture_output=True,text=True,timeout=8,check=True)
    result={}
    for block in run.stdout.strip().split('\n\n'):
        props=dict(line.split('=',1) for line in block.splitlines() if '=' in line)
        if props.get('Id'):result[props['Id']]=props
    return result

def context(data=DATA):
    try:
        with closing(sqlite3.connect('file:'+str(data/'catalogue.sqlite')+'?mode=ro',uri=True,timeout=2)) as conn:
            row=conn.execute("SELECT value FROM cache WHERE key='validation-worker' AND expires>?",(time.time(),)).fetchone()
            value=json.loads(row[0]) if row else {}
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
        enabled=timer.get('UnitFileState')=='enabled' and timer.get('ActiveState')=='active'
        if failed:health='🔴 Failed'
        elif not enabled:health='🟠 Timer disabled / stopped'
        elif running:health='🔵 Running'
        else:health='🟢 Healthy — waiting'
        detail=''
        if base=='media-source-availability':
            if running and current.get('key'):
                detail=f"Checking {current.get('name',current['key'])} · {span(time.time()-current.get('started',time.time()))} elapsed · batch {current.get('position',0)}"
            else:detail=current.get('message','Progress is saved on disk; successful checks are skipped')
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
        rows.append({'id':base,'name':name,'health':health,'schedule':schedule,
            'last_run':service.get('ExecMainStartTimestamp') or timer.get('LastTriggerUSec') or 'Never',
            'last_result':'In progress' if running else service.get('Result','Not run'),
            'next_run':next_run,'countdown':countdown,'detail':detail})
    return rows

def counts(data=DATA):
    now=time.time();cache=Path('/run/media-worker-counts.json')
    if data==DATA and cache.exists():
        try:
            saved=json.loads(cache.read_text())
            if now-saved['time']<60:return saved['rows']
        except (OSError,ValueError,KeyError):pass
    rows=[]
    def add(name,value,detail=''):rows.append({'id':str(len(rows)),'name':name,'value':f'{value:,}' if isinstance(value,int) else str(value),'detail':detail,'updated':stamp(now)})
    with closing(sqlite3.connect('file:'+str(data/'catalogue.sqlite')+'?mode=ro',uri=True,timeout=3)) as conn:
        grouped=dict(conn.execute('SELECT state,COUNT(*) FROM validation GROUP BY state'))
        add('Awaiting first check',grouped.get('pending',0),'Movie, first-episode and additional-episode source keys')
        add('Failed / not passed — retained for retry',grouped.get('unavailable',0),'Not deleted; retry waits increase up to 30 days')
        due=conn.execute("SELECT COUNT(*) FROM validation WHERE state='unavailable' AND next_check<=?",(now,)).fetchone()[0]
        add('Failed checks due for retry now',due,'One in five probes is reserved for due failures')
        add('Passed source / episode checks',grouped.get('verified',0),'Several episode checks can belong to one visible show')
        pending=dict(conn.execute("SELECT CASE WHEN key LIKE 'movie:%' THEN 'Movies' WHEN key LIKE '%:1:1' THEN 'Shows' ELSE 'Episodes' END,COUNT(*) FROM validation WHERE state='pending' GROUP BY 1"))
        for kind in ('Movies','Shows','Episodes'):add(kind+' awaiting first check',pending.get(kind,0),'Shows use S1E1 as the initial check; other episodes counted separately')
        distinct=conn.execute("SELECT COUNT(DISTINCT CASE WHEN key LIKE 'movie:%' THEN key END),COUNT(DISTINCT CASE WHEN key LIKE 'series:%' THEN substr(key,1,instr(substr(key,8),':')+6) END) FROM validation WHERE state='verified'").fetchone()
        add('Passed distinct movies',distinct[0]);add('Passed distinct shows',distinct[1])
        recent=conn.execute('SELECT COUNT(*),MAX(checked) FROM validation WHERE checked>?',(now-3600,)).fetchone()
        add('Keys with a completed check in the last hour',recent[0],'Latest result per key; includes failures, not a count of new visible titles')
        latest=conn.execute('SELECT MAX(checked) FROM validation').fetchone()[0]
        add('Most recent completed check',stamp(latest))
        sample=data.parent.parent/'media/movies/Tests/AAC Main TV test/AAC Main TV test.mp4'
        add('AAC Main TV test (Big Buck Bunny)', 'Ready for TV test; confirmation pending' if sample.is_file() else 'Test sample not available','Search AAC Main TV test in Jellyfin Movies; direct playback and sound must be checked on TV')
        conn.execute('ATTACH DATABASE ? AS bulk',('file:'+str(data/'imdb/catalogue.sqlite')+'?mode=ro',))
        total=conn.execute('SELECT COUNT(*) FROM bulk.titles').fetchone()[0]
        add('Metadata titles indexed locally',total,'Discovery metadata is not proof of playable video')
        outside=conn.execute("SELECT COUNT(*) FROM bulk.titles t WHERE NOT EXISTS(SELECT 1 FROM validation v WHERE v.key=t.kind||':'||t.imdb||CASE WHEN t.kind='series' THEN ':1:1' ELSE '' END)").fetchone()[0]
        add('Indexed titles outside the queue',outside,'Should normally be zero after discovery seeding')
        for kind in ('movie','series'):
            identity="substr(v.key,7)" if kind=='movie' else "substr(v.key,8,instr(substr(v.key,8),':')-1)"
            query="SELECT COUNT(DISTINCT t.imdb) FROM validation v JOIN bulk.titles t ON t.imdb="+identity+" WHERE v.state='verified' AND t.kind=?"
            query+=" AND NOT EXISTS(SELECT 1 FROM cache c WHERE c.key='source-status:'||v.key AND c.expires>strftime('%s','now') AND json_extract(c.value,'$.state')='unavailable' AND COALESCE(json_extract(c.value,'$.checked_at'),0)>v.checked)"
            columns={r[1] for r in conn.execute('PRAGMA bulk.table_info(titles)')}
            dates=conn.execute("SELECT 1 FROM sqlite_master WHERE name='release_dates'").fetchone()
            if dates:
                query+=" AND NOT EXISTS(SELECT 1 FROM release_dates d WHERE d.kind=t.kind AND d.imdb=t.imdb AND d.released>date('now','localtime'))"
                if {'year','votes'}<=columns:
                    query+=" AND (t.year<strftime('%Y','now','localtime') OR t.votes>0 OR EXISTS(SELECT 1 FROM release_dates d WHERE d.kind=t.kind AND d.imdb=t.imdb AND d.released<=date('now','localtime')))"
            visible=conn.execute(query,(kind,)).fetchone()[0]
            add('Verified indexed '+('movies' if kind=='movie' else 'shows'),visible,'Excludes known future releases and newer failed checks; saved-only media excluded')
    c=context(data)
    if c.get('message'):add('Validator activity',c.get('name') or c.get('key') or c['message'],c['message'])
    if data==DATA:
        import tempfile,os
        with tempfile.NamedTemporaryFile(mode='w',dir='/run',prefix='media-worker-counts-',delete=False) as output:
            json.dump({'time':now,'rows':rows},output);temporary=Path(output.name)
        temporary.replace(cache)
    return rows

if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('section',choices=('workers','counts'));args=parser.parse_args()
    print(json.dumps(workers() if args.section=='workers' else counts(),ensure_ascii=False))
