"""Performance invariants without relying on wall-clock speed or live services."""
import concurrent.futures
import copy
import json
from pathlib import Path
import sqlite3
import tempfile
import threading
import time
import unittest
from http.server import ThreadingHTTPServer
from unittest.mock import patch
import test_features as fixtures
a=fixtures.a

class StartupTests(unittest.TestCase):
    setUp=fixtures.FeatureTests.setUp

    def test_browse_cost_depends_on_ready_titles_not_metadata_pool(self):
        path=a.DATA/'imdb/catalogue.sqlite';path.parent.mkdir()
        with sqlite3.connect(path) as connection:
            connection.execute('CREATE TABLE titles(imdb TEXT PRIMARY KEY,kind TEXT,name TEXT,original TEXT,year INT,genres TEXT,votes INT)')
            connection.executemany('INSERT INTO titles VALUES (?,?,?,?,?,?,?)',
                ((f'tt{1000000+n}','movie',f'Film {n}','Original',2020,'Drama',n) for n in range(50000)))
        for imdb in ('tt1000001','tt1049999'):a.F.validation_result('movie:'+imdb,True)
        real=sqlite3.connect;steps=[0]
        def connect(*args,**kwargs):
            connection=real(*args,**kwargs)
            if 'imdb/catalogue.sqlite' in str(args[0]):
                def progress():steps[0]+=1000;return int(steps[0]>20000)
                connection.set_progress_handler(progress,1000)
            return connection
        with patch.object(a.sqlite3,'connect',side_effect=connect):result=a.F.browse('movie',limit=100)
        self.assertEqual(result['total'],2)
        self.assertEqual({m['id'] for m in result['metas']},{'tt1000001','tt1049999'})
        self.assertLessEqual(steps[0],20000,'A full metadata scan would exceed this instruction budget')

    def test_cached_title_metadata_survives_process_restart_without_bulk_reads(self):
        path=a.DATA/'imdb/catalogue.sqlite';path.parent.mkdir()
        with sqlite3.connect(path) as c:
            c.execute('CREATE TABLE titles(imdb TEXT PRIMARY KEY,kind TEXT,name TEXT,original TEXT,year INT,genres TEXT,votes INT)')
            c.execute("INSERT INTO titles VALUES ('tt1234567','movie','Fixture','Fixture',2020,'Drama',1)")
        a.F.validation_result('movie:tt1234567',True)
        self.assertEqual(a.F.browse('movie')['metas'][0]['name'],'Fixture')
        fresh=a.Features(a.__dict__)
        real=sqlite3.connect
        def connect(*args,**kwargs):
            c=real(*args,**kwargs)
            if 'imdb/catalogue.sqlite' in str(args[0]):
                c.set_authorizer(lambda action,table,*rest:sqlite3.SQLITE_DENY if action==sqlite3.SQLITE_READ and table=='titles' else sqlite3.SQLITE_OK)
            return c
        with patch.object(a.sqlite3,'connect',side_effect=connect):
            self.assertEqual(fresh.browse('movie')['metas'][0]['name'],'Fixture')
        with sqlite3.connect(path) as c:c.execute("UPDATE titles SET name='Changed' WHERE imdb='tt1234567'")
        self.assertEqual(fresh.browse('movie')['metas'][0]['name'],'Changed')

    def test_navigation_yield_does_not_mark_pending_source_failed(self):
        key='movie:tt1234567';a.F.enqueue(key)
        with patch.object(a.F,'jf',return_value=[]),patch.object(a,'prepared',side_effect=a.ClientCancelled('navigation')):
            a.F.warm_availability(1)
        with a.F.db() as c:row=c.execute('SELECT state,attempts,checked FROM validation WHERE key=?',(key,)).fetchone()
        self.assertEqual(row,('pending',0,0))

    def test_preflight_process_is_stopped_promptly_on_navigation(self):
        import sys
        began=time.monotonic()
        with a.request_budget(began+3,lambda:time.monotonic()-began>.05):
            with self.assertRaises(a.ClientCancelled):
                a.run_preflight([sys.executable,'-c','import time;time.sleep(10)'],capture_output=True,timeout=10)
        self.assertLess(time.monotonic()-began,3)

    def test_readiness_changes_take_effect_without_waiting_for_page_cache(self):
        path=a.DATA/'imdb/catalogue.sqlite';path.parent.mkdir()
        with sqlite3.connect(path) as c:
            c.execute('CREATE TABLE titles(imdb TEXT PRIMARY KEY,kind TEXT,name TEXT,original TEXT,year INT,genres TEXT,votes INT)')
            c.execute("INSERT INTO titles VALUES ('tt1234567','movie','Fixture','Fixture',2020,'Drama',1)")
        a.F.validation_result('movie:tt1234567',True)
        self.assertEqual(a.F.browse('movie')['total'],1)
        a.F.validation_result('movie:tt1234567',False)
        self.assertEqual(a.F.browse('movie')['metas'],[])

    def test_unchanged_discovery_does_not_take_writer_lock(self):
        metas=[{'id':'tt1234567','type':'movie','name':'Fixture'}]
        a.F.decorate_posters(copy.deepcopy(metas))
        # A different process can hold the only writer slot while unchanged
        # browsing still completes through a read-only transaction.
        with a.F.db() as writer:
            writer.execute('BEGIN IMMEDIATE')
            with concurrent.futures.ThreadPoolExecutor() as pool:
                result=pool.submit(a.F.decorate_posters,copy.deepcopy(metas)).result(timeout=3)
        self.assertIn('/poster/',result[0]['poster'])

    def test_local_poster_fast_path_and_conditional_delivery_need_no_database(self):
        folder=a.LIBRARY/'movies/tt1234567';folder.mkdir(parents=True)
        (folder/'poster.jpg').write_bytes(b'fixture jpeg bytes')
        (folder/'badge-style.txt').write_text(a.POSTER_STYLE);(folder/'badge.txt').write_text('HE')
        server=ThreadingHTTPServer(('127.0.0.1',0),a.Handler)
        thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
        try:
            url='http://127.0.0.1:'+str(server.server_port)+'/poster/movie/tt1234567.jpg?style='+a.POSTER_STYLE+'&badge=HE'
            with patch.object(a.F,'db',side_effect=AssertionError('Cached artwork should not open SQLite')),patch.object(a,'fetch',side_effect=AssertionError('Cached artwork should not use a provider')):
                with a.R.urlopen(url,timeout=2) as response:
                    self.assertEqual(response.read(),b'fixture jpeg bytes');etag=response.headers['ETag']
                    self.assertIn('private',response.headers['Cache-Control'])
                with self.assertRaises(a.urllib.error.HTTPError) as unchanged:
                    a.R.urlopen(a.R.Request(url,headers={'If-None-Match':etag}),timeout=2)
                self.assertEqual(unchanged.exception.code,304)
            self.assertIsNone(a.cached_poster(folder,{'style':[a.POSTER_STYLE],'badge':['EN']}))
            self.assertIsNone(a.cached_poster(folder,{'style':['old'],'badge':['HE']}))
            (folder/'poster.jpg').write_bytes(b'updated artwork')
            with a.R.urlopen(a.R.Request(url,headers={'If-None-Match':etag}),timeout=2) as response:
                self.assertEqual(response.status,200);self.assertNotEqual(response.headers['ETag'],etag)
        finally:server.shutdown();server.server_close();thread.join()

    def test_badges_only_decode_requested_titles(self):
        a.remember('availability:movie:tt1234567',{'languages':['heb']},100)
        # An unrelated malformed document must not be read/decoded for this page.
        with a.db() as c:c.execute('INSERT INTO cache VALUES (?,?,?)',('availability:movie:tt7654321','broken',time.time()+100))
        self.assertEqual(a.poster_badges([{'id':'tt1234567','type':'movie'}]),{('movie','tt1234567'):'HE'})

if __name__=='__main__':unittest.main()
