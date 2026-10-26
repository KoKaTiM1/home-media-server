import importlib.util
import json
from pathlib import Path
import sqlite3
import tempfile
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

if __name__=='__main__':unittest.main()
