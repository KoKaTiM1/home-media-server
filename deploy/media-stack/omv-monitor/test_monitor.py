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
    def test_lan_page_prioritizes_current_verifier_and_gateway(self):
        units={'media-verification-coordinator.service':{'ActiveState':'active','Result':'success','UnitFileState':'enabled'}}
        with patch.object(m,'systemd',return_value=units),patch.object(m,'context',return_value={}):rows=m.workers()
        row=next(r for r in rows if r['id']=='media-verification-coordinator')
        self.assertIn('Running',row['health']);self.assertEqual(row['next_run'],'Continuous service')
        self.assertLessEqual(len(rows),10)
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
                c.execute('CREATE TABLE validation(key TEXT PRIMARY KEY,state TEXT,next_check REAL,checked REAL,proof TEXT)')
                c.execute('CREATE TABLE cache(key TEXT PRIMARY KEY,value TEXT,expires REAL)')
                c.execute('CREATE TABLE manual_rechecks(key TEXT PRIMARY KEY,status TEXT)')
                c.executemany('INSERT INTO manual_rechecks VALUES (?,?)',[('movie:tt1000004','pending'),('movie:tt1000003','failed'),('movie:tt1000001','passed')])
                c.execute('CREATE TABLE audio_recheck(key TEXT PRIMARY KEY,completed REAL)')
                c.execute("INSERT INTO audio_recheck VALUES ('movie:tt1000001',0)")
                c.execute('INSERT INTO cache VALUES (?,?,?)',('audio-policy-resync-v1',json.dumps({'scheduled_at':1,'failed_scheduled':3}),9999999999))
                c.executemany('INSERT INTO validation VALUES (?,?,?,?,?)',[
                    ('movie:tt1000001','verified',0,1,None),
                    ('series:tt1000002:1:1','verified',0,1,None),
                    ('series:tt1000002:1:2','verified',0,1,None),
                    ('movie:tt1000003','unavailable',0,1,None),
                    ('movie:tt1000004','pending',0,0,None)])
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
            self.assertEqual(values['Previously failed entries scheduled for audio resync'],'3')
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
                'playable':20,'v5':12,'v5_hour':3,'audit_done':7,'audit_total':10}}))
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
            self.assertEqual(values['Final v5 verified']['value'],'12')
            self.assertEqual(values['Not yet v5 verified']['value'],'88')
            self.assertEqual(values['Windows final passes']['value'],'4')
            self.assertEqual(values['Windows decode queue']['value'],'1 active / 0 ready')
            self.assertIn('Decoding later sample',values['Windows working on']['detail'])
            self.assertIn('3.0 MiB received',values['Windows working on']['detail'])
            self.assertIn('Run 8',values['Windows batch']['value'])
            self.assertIn('transport',values['Latest PC operation issue']['detail'])
            self.assertNotIn('private-signed-url',str(rows))

    def test_completed_initial_audit_is_replaced_by_current_ready_counts(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);data=root/'catalogue';data.mkdir()
            snapshot=root/'snapshot.json';snapshot.write_text(json.dumps({'time':time.time(),'summary':{
                'total':100,'pending':70,'unavailable':5,'v5':25,'v5_hour':2,
                'audit_done':10,'audit_total':10,'ready_movies':15,'ready_shows':7}}))
            rows=m.overview(data,snapshot)
            values={row['name']:row['value'] for row in rows}
            self.assertNotIn('Initial v5 audit',values)
            self.assertEqual(values['Verified movies'],'15')
            self.assertEqual(values['Verified shows'],'7')

if __name__=='__main__':unittest.main()
