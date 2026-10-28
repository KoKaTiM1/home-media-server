"""Bounded, sanitized foreground failures and server-authoritative rechecks."""
import re
import time
import uuid
from reliability import SourceFailure

KEY=re.compile(r'(movie:tt\d{5,12}|series:tt\d{5,12}:\d+:\d+)')
CATEGORIES={'upstream_http','client_disconnect','timeout','local_database','network',
            'local_io','operation','decode_failure','source_stalled','source_failure','checkpoint','playback_busy'}

class PlaybackDiagnostics:
    def __init__(self,g):self.g=g
    def recheck(self,key,error):
        if not KEY.fullmatch(key) or not isinstance(error,SourceFailure):return False
        now=time.time()
        with self.g['F'].db() as c:
            row=c.execute('SELECT status FROM manual_rechecks WHERE key=?',(key,)).fetchone()
            if row and row[0]=='pending':return False
            ident=uuid.uuid4().hex
            c.execute("INSERT INTO validation(key,state,priority,next_check,reason) VALUES (?,'pending',1000,0,'Confirmed playback failure; fresh source check required') ON CONFLICT(key) DO UPDATE SET state='pending',priority=MAX(priority,1000),next_check=0,reason=excluded.reason",(key,))
            c.execute("INSERT INTO manual_rechecks VALUES (?,?,?,'pending') ON CONFLICT(key) DO UPDATE SET requested=excluded.requested,request_id=excluded.request_id,status='pending'",(key,now,ident))
            c.execute('INSERT INTO recheck_requests VALUES (?,?,1)',(ident,now))
            for prefix in ('prepared:','verified-prepared:'):
                c.execute('DELETE FROM cache WHERE key=? OR (key>=? AND key<?)',(prefix+key,prefix+key+':',prefix+key+':\uffff'))
            c.execute('DELETE FROM cache WHERE key IN (?,?)',('resolved:'+key,'source-status:'+key))
        return True
    def record(self,event):
        key=event.get('key','')
        if not KEY.fullmatch(key) or event.get('outcome') not in ('failed','cancelled','checkpoint'):return
        phase=event.get('phase','')
        if not re.fullmatch('[a-z_]{1,40}',phase):return
        category=event.get('error_category') or 'client_disconnect'
        if category not in CATEGORIES:category='operation'
        error_type=event.get('error_type') or ''
        if not re.fullmatch('[A-Za-z_][A-Za-z0-9_]{0,63}',error_type):error_type=''
        now=time.time()
        with self.g['F'].db() as c:
            c.execute('CREATE TABLE IF NOT EXISTS playback_events(id INTEGER PRIMARY KEY,created REAL NOT NULL,key TEXT NOT NULL,phase TEXT NOT NULL,category TEXT NOT NULL,outcome TEXT NOT NULL,error_type TEXT NOT NULL,seconds REAL,http_status INTEGER)')
            c.execute('INSERT INTO playback_events(created,key,phase,category,outcome,error_type,seconds,http_status) VALUES (?,?,?,?,?,?,?,?)',
                      (now,key,phase,category,event['outcome'],error_type,max(0,min(86400,float(event.get('seconds',0)))),event.get('http_status') if isinstance(event.get('http_status'),int) else None))
            c.execute('DELETE FROM playback_events WHERE created<?',(now-30*86400,))
            c.execute('DELETE FROM playback_events WHERE id NOT IN (SELECT id FROM playback_events ORDER BY id DESC LIMIT 200)')
