import importlib.util
import json
from pathlib import Path
import sqlite3
import tempfile
import time
import unittest
from unittest.mock import patch
spec=importlib.util.spec_from_file_location('monitor',Path(__file__).with_name('monitor.py'))
m=importlib.util.module_from_spec(spec);spec.loader.exec_module(m)

class MonitorTests(unittest.TestCase):
    def test_playback_events_show_current_recheck_and_hide_expired_events(self):
        with tempfile.TemporaryDirectory() as directory:
            data=Path(directory)
            with sqlite3.connect(data/'catalogue.sqlite') as c:
                c.execute('CREATE TABLE playback_events(id INTEGER PRIMARY KEY,created REAL,key TEXT,phase TEXT,category TEXT,outcome TEXT,http_status INTEGER)')
                c.execute('CREATE TABLE manual_rechecks(key TEXT PRIMARY KEY,status TEXT)')
                c.execute('CREATE TABLE items(key TEXT PRIMARY KEY,meta TEXT)')
                c.execute('INSERT INTO items VALUES (?,?)',('movie:tt1234567',json.dumps({'name':'Fixture title'})))
                c.execute('INSERT INTO manual_rechecks VALUES (?,?)',('movie:tt1234567','pending'))
                c.executemany('INSERT INTO playback_events VALUES (?,?,?,?,?,?,?)',[
                    (1,time.time()-31*86400,'movie:tt1234567','request','timeout','failed',None),
                    (2,time.time(),'movie:tt1234567','video_probe','decode_failure','failed',None)])
            rows=m.playback_events(data)
            self.assertEqual(len(rows),1);self.assertEqual(rows[0]['title'],'Fixture title')
            self.assertEqual(rows[0]['recheck'],'pending');self.assertEqual(rows[0]['category'],'decode failure')
            self.assertNotIn('url',rows[0])
    def test_dynamic_registry_counts_distinct_torrent_backends(self):
        import sys
        sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'catalogue'))
        from verification_jobs import DecodeJobs
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);data=root/'catalogue';data.mkdir()
            q=DecodeJobs(root/'verification-worker'/'queue.sqlite');q.initialize();q.configure(8,2)
            q.set('local_torrent_enabled',True);q.set('local_torrent_permitted',True)
            for i in range(1,9):q.seen('fixture-'+str(i),i);q.backend_health('fixture-'+str(i),True,'torrent-'+str(1+(i-1)//4))
            with q.db(True) as c:
                now=time.time()
                c.execute("INSERT INTO decode_jobs(id,key,operation,state,created,claimed_at,finished,outcome,checkpoint_code,execution_seconds,lock_wait_seconds,active_seconds) VALUES ('diagnostic','movie:tt0000001','verify_title','cancelled',?,?,?,?,?,480,12,40)",(now-90,now-80,now,'checkpoint','cache_attribution'))
                c.execute("INSERT INTO decode_jobs(id,key,operation,state,created,finished,outcome,cancel_code) VALUES ('cancel','movie:tt0000002','verify_title','cancelled',?,?,'cancelled','queue_timeout')",(now-480,now))
            rows=m.overview(data,root/'missing.json');by={r['name']:r for r in rows}
            self.assertIn('cache_attribution: 1',by['Windows speed checkpoint reasons']['value'])
            self.assertIn('queue_timeout: 1',by['Windows cancellation reasons']['value'])
            self.assertIn('10.0s queued / 12.0s lock wait / 40.0s active',by['Windows time per reported attempt']['value'])
            self.assertEqual(by['Windows lanes']['value'],'8 / 8')
            self.assertIn('2 healthy TorrServers',by['Windows local torrent']['value'])
            self.assertIn('8 workers / 2 TorrServers',by['Windows requested topology']['value'])

    def test_lan_page_prioritizes_current_verifier_and_gateway(self):
        units={'media-verification-coordinator.service':{'ActiveState':'active','Result':'success','UnitFileState':'enabled'}}
        with patch.object(m,'systemd',return_value=units),patch.object(m,'context',return_value={}):rows=m.workers()
        row=next(r for r in rows if r['id']=='media-verification-coordinator')
        self.assertIn('Running',row['health']);self.assertEqual(row['next_run'],'Continuous service')
        self.assertEqual(len(rows),len(m.JOBS))
        self.assertEqual(len({r['id'] for r in rows}),len(rows))
        self.assertEqual(rows[1]['name'],'Windows LAN source verification')
        self.assertFalse(any('LAN metadata' in r['name'] for r in rows))

    def test_running_oneshot_is_running_not_failure_and_disabled_timer_warns(self):
        units={}
        for base,_,_ in m.JOBS:
            units[base+'.service']={'ActiveState':'inactive','Result':'success'}
            units[base+'.timer']={'UnitFileState':'enabled','ActiveState':'active'}
        units['media-source-availability.service']['ActiveState']='activating'
        units['media-cleanup.timer']['ActiveState']='inactive'
        with patch.object(m,'systemd',return_value=units),patch.object(m,'context',return_value={}):rows=m.workers()
        self.assertIn('Running',rows[0]['health']);self.assertIn('Healthy',rows[1]['health'])
        self.assertIn('stopped',next(row for row in rows if row['id']=='media-cleanup')['health'])

    def test_navigation_reuses_snapshot_without_opening_database(self):
        expected=[{'name':'Fixture snapshot','value':'42'}]
        with patch.object(m,'foreground_busy',return_value=True),patch.object(m.Path,'read_text',return_value=json.dumps({'rows':expected})),patch.object(m.sqlite3,'connect',side_effect=AssertionError('Navigation must not start heavy counter reads')):
            self.assertEqual(m.counts(refresh=True),expected)

    def test_counts_separate_episodes_titles_failures_and_unqueued_metadata(self):
        with tempfile.TemporaryDirectory() as directory:
            data=Path(directory);(data/'imdb').mkdir()
            with sqlite3.connect(data/'catalogue.sqlite') as c:
                c.execute('CREATE TABLE validation(key TEXT PRIMARY KEY,state TEXT,next_check REAL,checked REAL,proof TEXT,reason TEXT)')
                c.execute('CREATE TABLE cache(key TEXT PRIMARY KEY,value TEXT,expires REAL)')
                c.execute('CREATE TABLE manual_rechecks(key TEXT PRIMARY KEY,status TEXT)')
                c.executemany('INSERT INTO manual_rechecks VALUES (?,?)',[('movie:tt1000004','pending'),('movie:tt1000003','failed'),('movie:tt1000001','passed')])
                c.execute('CREATE TABLE audio_recheck(key TEXT PRIMARY KEY,completed REAL)')
                c.execute("INSERT INTO audio_recheck VALUES ('movie:tt1000001',0)")
                c.execute('INSERT INTO cache VALUES (?,?,?)',('audio-policy-resync-v1',json.dumps({'scheduled_at':1,'failed_scheduled':3}),9999999999))
                c.executemany('INSERT INTO validation VALUES (?,?,?,?,?,?)',[
                    ('movie:tt1000001','verified',0,1,None,''),
                    ('series:tt1000002:1:1','verified',0,1,None,''),
                    ('series:tt1000002:1:2','verified',0,1,None,''),
                    ('movie:tt1000003','unavailable',0,1,None,'no_source'),
                    ('movie:tt1000004','pending',0,0,None,'')])
            with sqlite3.connect(data/'imdb/catalogue.sqlite') as c:
                c.execute('CREATE TABLE titles(imdb TEXT PRIMARY KEY,kind TEXT)')
                c.executemany('INSERT INTO titles VALUES (?,?)',[(f'tt100000{i}','series' if i==2 else 'movie') for i in range(1,6)])
            values={r['name']:r['value'] for r in m.counts(data)}
            self.assertEqual(values['Passed source / episode checks'],'3')
            self.assertEqual(values['Passed distinct shows'],'1')
            self.assertEqual(values['Verified indexed shows'],'1')
            self.assertEqual(values['Verified indexed movies'],'1')
            self.assertEqual(values['Failed checks due for retry now'],'1')
            self.assertEqual(values['Indexed titles outside the queue'],'1')
            self.assertEqual(values['Passed sources awaiting new audio-policy audit'],'1')
            self.assertEqual(values['User reports awaiting fresh check'],'1')
            self.assertEqual(values['User reports failed latest check'],'1')
            self.assertEqual(values['User reports passed fresh check'],'1')
    def test_running_progress_shows_useful_data_and_audio_mode(self):
        units={}
        for base,_,_ in m.JOBS:
            units[base+'.service']={'ActiveState':'activating','Result':'success'}
            units[base+'.timer']={'UnitFileState':'enabled','ActiveState':'active'}
        current={'key':'movie:tt00000000','position':1,'preparation':{'phase':'later sample','received_bytes':12345,'buffer_bytes':4096,'speed':1234,'audio':'AAC-LC conversion'}}
        with patch.object(m,'systemd',return_value=units),patch.object(m,'context',return_value=current):rows=m.workers()
        row=next(r for r in rows if r['id']=='media-source-availability')
        text=str(row)
        for word in ('later sample','12,345','4,096','AAC-LC conversion'):self.assertIn(word,text)

    def test_overview_uses_final_title_counts_and_live_lease_progress(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);data=root/'catalogue';data.mkdir();queue=root/'verification-worker';queue.mkdir()
            snapshot=root/'snapshot.json';now=time.time()
            snapshot.write_text(json.dumps({'time':now,'summary':{'total':100,'pending':75,'unavailable':5,
                'playable':20,'v6':12,'v6_hour':3,'v6_audit_done':7,'v6_audit_total':10,
                'v6_audit_pending':3,'v6_audit_passed':5,'v6_audit_failed':2}}))
            with sqlite3.connect(queue/'queue.sqlite') as c:
                c.execute('CREATE TABLE totals(key TEXT,value INTEGER)')
                c.execute("INSERT INTO totals VALUES('windows_titles_verified',4)")
                c.execute('CREATE TABLE control(key TEXT,value TEXT)')
                c.executemany('INSERT INTO control VALUES (?,?)',[('enabled','true'),('reason','"Verification permitted"')])
                c.execute('CREATE TABLE client(id INTEGER,seen REAL)');c.execute('INSERT INTO client VALUES(1,?)',(now,))
                c.execute('CREATE TABLE decode_jobs(key TEXT,operation TEXT,params TEXT,state TEXT,created REAL,stream_started REAL,phase TEXT,phase_at REAL,controller REAL,outcome TEXT,reason_code TEXT,finished REAL,url TEXT)')
                c.execute('INSERT INTO decode_jobs VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)',('series:tt1234567:1:2','sample',json.dumps({'position':140}),'leased',now-20,now-15,'sample decode',now-10,now+30,'','',0,'private-signed-url'))
                c.execute('INSERT INTO decode_jobs VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)',('movie:tt1234568','probe','{}','consumed',now-120,0,'video probe',now-100,0,'checkpoint','transport',now-90,''))
            with sqlite3.connect(queue/'queue.sqlite') as c:
                c.execute('ALTER TABLE decode_jobs ADD COLUMN deadline REAL DEFAULT 9999999999')
            activity={'key':'series:tt1234567:1:2','name':'Example Show S1E2','position':2,'batch':8,'preparation':{'received_bytes':3145728}}
            with patch.object(m,'context',return_value=activity):rows=m.overview(data,snapshot)
            values={row['name']:row for row in rows}
            self.assertEqual(values['V6 verified sources']['value'],'12')
            self.assertEqual(values['Not yet V6 verified']['value'],'88')
            self.assertEqual(values['V6 speed audit']['value'],'7 / 10')
            self.assertEqual(values['V6 passes in last hour']['value'],'3')
            self.assertEqual(values['Windows final passes']['value'],'4')
            self.assertEqual(values['Windows decode queue']['value'],'1 active / 0 ready')
            self.assertIn('Decoding later sample',values['Windows working on']['detail'])
            self.assertIn('3.0 MiB received',values['Windows working on']['detail'])
            self.assertIn('Run 8',values['Windows batch']['value'])
            self.assertIn('transport',values['Latest PC operation issue']['detail'])
            self.assertNotIn('private-signed-url',str(rows))

    def test_overview_uses_v6_counts_and_hides_legacy_audit_status(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);data=root/'catalogue';data.mkdir()
            snapshot=root/'snapshot.json';snapshot.write_text(json.dumps({'time':time.time(),'summary':{
                'total':100,'pending':70,'unavailable':5,'v6':25,'v6_hour':2,
                'v6_audit_done':10,'v6_audit_total':10,'v6_audit_pending':0,
                'v6_audit_passed':9,'v6_audit_failed':1,'initial_v5_done':946,'initial_v5_total':946,
                'ready_movies':15,'ready_shows':7}}))
            rows=m.overview(data,snapshot)
            values={row['name']:row['value'] for row in rows}
            self.assertNotIn('Initial v5 audit',values)
            self.assertNotIn('V5',str(rows))
            self.assertEqual(values['V6 verified sources'],'25')
            self.assertEqual(values['V6 speed audit'],'10 / 10')
            self.assertEqual(values['Verified movies'],'15')
            self.assertEqual(values['Verified shows'],'7')

if __name__=='__main__':unittest.main()
