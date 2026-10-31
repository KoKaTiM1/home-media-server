"""Separate metadata preparation: bounded admission and exclusive fetching."""
import json,time,unittest
from unittest.mock import patch
import test_metadata_lan as fixtures
app=fixtures.app
KEY='series:tt0000001:1:1'
class PoolTests(unittest.TestCase):
    def setUp(self):
        fixtures.ImportTests.setUp(self)
        self.old_key=getattr(app.BUDGET,'preparation_key',None)
        app.REL.db().close();app.F.start_verification_sweep()
        self.configure()
    def tearDown(self):
        app.BUDGET.preparation_key=self.old_key
        fixtures.ImportTests.tearDown(self)
    def configure(self,high=256,low=128):
        (app.DATA/'metadata-preparation.json').write_text(json.dumps({'enabled':True,'concurrency':2,'high':high,'low':low}))
    def add(self,key):
        with app.F.db() as c:c.execute("INSERT OR IGNORE INTO validation(key,state) VALUES (?,'pending')",(key,))
    def test_producer_queues_instead_of_fetching_missing_metadata(self):
        app.BUDGET.metadata_background=True;app.BUDGET.preparation_key=KEY
        with patch.object(app,'fetch',return_value={'meta':fixtures.result()['metadata']}) as fetch:
            with self.assertRaises(app.MetadataPending):app.metadata('series','tt0000001')
        self.assertEqual(fetch.call_count,0)
        with app.F.db() as c:self.assertEqual(c.execute('SELECT key FROM metadata_preparation_requests').fetchone()[0],KEY)
    def test_atomic_parent_admission_hysteresis_and_cached_work(self):
        self.configure(2,1)
        second='movie:tt0000002';third='movie:tt0000003'
        self.add(second);self.add(third)
        self.assertFalse(app.F.metadata_admission_blocked(KEY))
        self.assertFalse(app.F.metadata_admission_blocked(second))
        self.assertTrue(app.F.metadata_admission_blocked(third))
        # A sibling needs the same parent and consumes no new parent capacity.
        sibling='series:tt0000001:1:2';self.add(sibling)
        self.assertFalse(app.F.metadata_admission_blocked(sibling))
        app.remember('meta:'+third,{'id':'tt0000003'},300)
        self.assertFalse(app.F.metadata_admission_blocked(third))
        with app.F.db() as c:
            self.assertEqual(c.execute('SELECT count(distinct metadata_key) FROM metadata_preparation_requests').fetchone()[0],2)
            c.execute('DELETE FROM metadata_preparation_requests WHERE metadata_key=?',('meta:'+second,))
        fourth='movie:tt0000004';self.add(fourth)
        self.assertFalse(app.F.metadata_admission_blocked(fourth))
    def test_existing_backlog_is_retained_and_ready_import_can_resume(self):
        self.configure(2,1)
        for i in range(1,5):
            key=KEY if i==1 else 'movie:tt000000'+str(i)
            self.add(key)
            # Existing requests are retained when the new cap is lower.
            with app.F.db() as c:c.execute('INSERT INTO metadata_preparation_requests VALUES (?,?,?,0)',(key,'meta:'+':'.join(key.split(':')[:2]),time.time()))
        extra='movie:tt0000005';self.add(extra)
        self.assertTrue(app.F.metadata_admission_blocked(extra))
        with app.F.db() as c:self.assertEqual(c.execute('SELECT count(*) FROM metadata_preparation_requests').fetchone()[0],4)
    def test_pool_request_fetch_import_and_parent_dedup(self):
        self.assertFalse(app.F.metadata_admission_blocked(KEY))
        with patch.object(app,'fetch',return_value={'meta':fixtures.result()['metadata']}) as fetch:
            self.assertTrue(app.fetch_waiting_metadata(self.jobs))
            self.assertEqual(app.metadata_import(),1)
            with self.jobs.db(True) as c:self.jobs.put(c,'provider_slot',0)
            self.assertIsNone(app.fetch_waiting_metadata(self.jobs))
        self.assertEqual(fetch.call_count,1)
        with app.F.db() as c:
            self.assertEqual(c.execute('SELECT count(*) FROM metadata_preparation_requests').fetchone()[0],0)
            self.assertEqual(c.execute('SELECT state,attempts FROM validation').fetchone(),('pending',0))
    def test_slot_contention_does_not_defer_parent_for_two_minutes(self):
        self.assertFalse(app.F.metadata_admission_blocked(KEY))
        with self.jobs.db(True) as c:self.jobs.put(c,'provider_slot',time.time()+60)
        self.assertFalse(app.fetch_waiting_metadata(self.jobs))
        with app.F.db() as c:self.assertEqual(c.execute('SELECT count(*) FROM verification_metadata_fetch').fetchone()[0],0)
    def test_old_import_timer_does_not_fetch_when_pool_enabled(self):
        self.assertFalse(app.F.metadata_admission_blocked(KEY))
        with patch.object(app,'fetch_waiting_metadata',side_effect=AssertionError('pool owns fetching')),patch('metadata_jobs.MetadataJobs.seed'):
            self.assertEqual(app.metadata_import(seed=True),0)
    def test_changed_proof_or_downloaded_request_is_not_fetched(self):
        self.assertFalse(app.F.metadata_admission_blocked(KEY))
        with app.F.db() as c:c.execute("UPDATE validation SET state='downloaded' WHERE key=?",(KEY,))
        with patch.object(app,'fetch',side_effect=AssertionError('downloaded title')):
            self.assertIsNone(app.fetch_waiting_metadata(self.jobs))
        with app.F.db() as c:self.assertEqual(c.execute('SELECT count(*) FROM metadata_preparation_requests').fetchone()[0],0)

    def test_two_slots_do_not_fetch_the_same_parent(self):
        self.assertFalse(app.F.metadata_admission_blocked(KEY))
        sibling='series:tt0000001:1:2';self.add(sibling)
        self.assertFalse(app.F.metadata_admission_blocked(sibling))
        from concurrent.futures import ThreadPoolExecutor
        with patch.object(app,'fetch',return_value={'meta':fixtures.result()['metadata']}) as fetch:
            with ThreadPoolExecutor(max_workers=2) as executor:
                results=list(executor.map(lambda _:app.fetch_waiting_metadata(self.jobs),range(2)))
        self.assertEqual(sum(bool(x) for x in results),1);self.assertEqual(fetch.call_count,1)
        self.assertEqual(app.metadata_import(),1)
        with app.F.db() as c:self.assertEqual(c.execute('SELECT count(*) FROM metadata_preparation_requests').fetchone()[0],0)
    def test_service_yields_to_playback_and_stops_cooperatively(self):
        def stop(seconds):(app.DATA/'metadata-prepare-1.stop').touch()
        with patch.object(app,'jf',return_value=[{'NowPlayingItem':{'Id':'fixture'}}]),patch.object(app,'fetch_waiting_metadata',side_effect=AssertionError('playback must pause')),patch.object(app.time,'sleep',side_effect=stop):
            app.metadata_prepare(1)
        with app.F.db() as c:self.assertEqual(c.execute('SELECT count(*) FROM metadata_preparation_workers').fetchone()[0],0)
    def test_full_first_sweep_admission_wait_is_not_terminal(self):
        self.configure(2,1)
        for i in range(2,4):
            key='movie:tt000000'+str(i);self.add(key)
            self.assertFalse(app.F.metadata_admission_blocked(key))
        candidate,waiting=app.F.first_sweep_candidate()
        self.assertIsNone(candidate);self.assertTrue(waiting)
        with app.F.db() as c:
            self.assertEqual(c.execute('SELECT completed FROM verification_sweeps').fetchone()[0],0)
            self.assertEqual(c.execute('SELECT outcome FROM verification_sweep_attempts WHERE key=?',(KEY,)).fetchone()[0],'queued')
        app.remember('meta:series:tt0000001',fixtures.result()['metadata'],300)
        self.assertEqual(app.F.first_sweep_candidate()[0][1],KEY)

    def test_slot_disabled_by_configuration_never_fetches(self):
        with patch.object(app,'fetch_waiting_metadata',side_effect=AssertionError('extra slot')):
            app.metadata_prepare(3)
    def test_manual_request_can_enter_while_discovery_is_paused(self):
        self.configure(2,1)
        for i in range(2,4):
            key='movie:tt000000'+str(i);self.add(key)
            self.assertFalse(app.F.metadata_admission_blocked(key))
        self.assertTrue(app.F.metadata_admission_blocked(KEY))
        with app.F.db() as c:c.execute("INSERT INTO manual_rechecks VALUES (?,?,?,'pending')",(KEY,time.time(),'fixture'))
        self.assertFalse(app.F.metadata_admission_blocked(KEY))
    def test_downloaded_title_does_not_consume_new_admission(self):
        with app.F.db() as c:c.execute("UPDATE validation SET state='downloaded' WHERE key=?",(KEY,))
        # Admission must not enqueue downloaded data even without a metadata cache.
        self.assertTrue(app.F.metadata_admission_blocked(KEY))
        with app.F.db() as c:self.assertEqual(c.execute('SELECT count(*) FROM metadata_preparation_requests').fetchone()[0],0)

    def test_manual_v6_metadata_fetch_does_not_change_saved_proof(self):
        proof=json.dumps({'version':6,'fixture':'accepted'})
        with app.F.db() as c:
            c.execute("UPDATE validation SET state='verified',proof=? WHERE key=?",(proof,KEY))
            c.execute("INSERT INTO manual_rechecks VALUES (?,?,?,'pending')",(KEY,time.time(),'fixture'))
        self.assertFalse(app.F.metadata_admission_blocked(KEY))
        with patch.object(app,'fetch',return_value={'meta':fixtures.result()['metadata']}):
            self.assertTrue(app.fetch_waiting_metadata(self.jobs));app.metadata_import()
        with app.F.db() as c:
            self.assertEqual(c.execute('SELECT state,proof FROM validation WHERE key=?',(KEY,)).fetchone(),('verified',proof))

