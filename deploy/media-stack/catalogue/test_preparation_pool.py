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
            c.execute("UPDATE metadata_preparation_control SET value=0 WHERE key='checked'")
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


    def test_pool_leaves_a_provider_slot_for_catalogue_pages(self):
        for i in range(2,7):
            key='movie:tt000000'+str(i);self.add(key)
            self.assertFalse(app.F.metadata_admission_blocked(key))
        def fetch(url,**kwargs):
            return {'meta':fixtures.result(kind='movie',imdb=url.rsplit('/',1)[1].split('.')[0])['metadata']}
        with patch.object(app,'fetch',side_effect=fetch):
            for i in range(4):
                with self.jobs.db(True) as c:self.jobs.put(c,'provider_slot',0)
                self.assertTrue(app.fetch_waiting_metadata(self.jobs));app.metadata_import()
            with self.jobs.db(True) as c:
                self.jobs.put(c,'provider_slot',0)
                self.assertGreater(self.jobs.value(c,'preparation_yield_until'),time.time())
            self.assertFalse(app.fetch_waiting_metadata(self.jobs))
        self.assertEqual(self.jobs.reserve_provider(),0)

    def test_paused_admission_polls_without_taking_a_writer_lock(self):
        self.configure(2,1)
        for i in range(2,4):
            key='movie:tt000000'+str(i);self.add(key)
            self.assertFalse(app.F.metadata_admission_blocked(key))
        self.assertTrue(app.F.metadata_admission_blocked(KEY))
        with app.F.db() as c:
            c.commit();before=c.total_changes
            self.assertTrue(app.F.metadata_admission_blocked(KEY,c))
            self.assertEqual(c.total_changes,before);self.assertFalse(c.in_transaction)
    def test_concurrent_discovery_does_not_over_admit_parents(self):
        self.configure(2,1)
        keys=['movie:tt00000'+str(i).zfill(2) for i in range(2,18)]
        for key in keys:self.add(key)
        from concurrent.futures import ThreadPoolExecutor
        with ThreadPoolExecutor(max_workers=4) as executor:
            results=list(executor.map(app.F.metadata_admission_blocked,keys))
        self.assertEqual(sum(not blocked for blocked in results),2)
        with app.F.db() as c:self.assertEqual(c.execute('SELECT count(distinct metadata_key) FROM metadata_preparation_requests').fetchone()[0],2)
    def test_downloaded_queue_entry_does_not_prevent_sweep_completion(self):
        with app.F.db() as c:c.execute("UPDATE validation SET state='downloaded' WHERE key=?",(KEY,))
        self.assertEqual(app.F.first_sweep_candidate(),(None,False))
        with app.F.db() as c:
            self.assertEqual(c.execute('SELECT state FROM validation').fetchone()[0],'downloaded')
            self.assertEqual(c.execute('SELECT outcome FROM verification_sweep_attempts').fetchone()[0],'deferred')

    def test_stop_waits_for_old_slot_lock_before_restart(self):
        import threading,fcntl
        locked=threading.Event();finished=threading.Event()
        def old_worker():
            with (app.DATA/'metadata-prepare-1.lock').open('a') as lock:
                fcntl.flock(lock,fcntl.LOCK_EX);locked.set()
                until=time.monotonic()+3
                while not (app.DATA/'metadata-prepare-1.stop').exists():
                    if time.monotonic()>until:return
                    time.sleep(.01)
                time.sleep(.15)
            finished.set()
        thread=threading.Thread(target=old_worker);thread.start();self.assertTrue(locked.wait(2))
        try:
            app.metadata_prepare_stop(1)
            self.assertTrue(finished.wait(.1))
        finally:thread.join(4)
    def test_busy_slot_fails_for_supervisor_retry_and_retains_stop_flag(self):
        import fcntl
        stop=app.DATA/'metadata-prepare-1.stop';stop.touch()
        with (app.DATA/'metadata-prepare-1.lock').open('a') as lock:
            fcntl.flock(lock,fcntl.LOCK_EX)
            with self.assertRaises(RuntimeError):app.metadata_prepare(1)
        self.assertTrue(stop.exists())

class SourcePacingTests(unittest.TestCase):
    def setUp(self):fixtures.ImportTests.setUp(self)
    def tearDown(self):fixtures.ImportTests.tearDown(self)
    def test_concurrent_producer_cannot_issue_second_request(self):
        import threading
        entered=threading.Event();release=threading.Event();errors=[]
        def request():
            try:
                with app.source_provider_slot():entered.set();release.wait(5)
            except Exception as error:errors.append(type(error).__name__)
        thread=threading.Thread(target=request);thread.start()
        try:
            self.assertTrue(entered.wait(2))
            with self.assertRaises(app.Checkpoint):
                with app.source_provider_slot():self.fail('Concurrent upstream call')
        finally:release.set();thread.join(5)
        self.assertEqual(errors,[])
        with self.assertRaises(app.Checkpoint):
            with app.source_provider_slot():self.fail('Missing pacing delay')
    def test_rate_limit_has_one_recovery_request_and_preserves_retry_after(self):
        import urllib.error
        from email.message import Message
        headers=Message();headers['Retry-After']='1200'
        with patch.object(app.time,'time',return_value=1000):
            with self.assertRaises(urllib.error.HTTPError):
                with app.source_provider_slot():raise urllib.error.HTTPError('https://example.invalid',429,'limited',headers,None)
            state=app.cached('source-provider-control');self.assertEqual(state['cooldown'],2200)
        with patch.object(app.time,'time',return_value=2199):
            with self.assertRaises(app.Checkpoint):
                with app.source_provider_slot():self.fail('Cooldown bypassed')
        with patch.object(app.time,'time',return_value=2201):
            with app.source_provider_slot():pass
            with self.assertRaises(app.Checkpoint):
                with app.source_provider_slot():self.fail('Recovery burst')
    def test_metadata_request_does_not_use_source_gate(self):
        with patch.object(app,'source_provider_slot',side_effect=AssertionError('wrong provider')),patch.object(app,'fetch_raw',return_value={'ok':True}):
            self.assertEqual(app.fetch('https://v3-cinemeta.strem.io/meta/movie/tt0000001.json'),{'ok':True})
    def test_pacing_wait_keeps_first_sweep_queued(self):
        app.REL.db().close()
        app.F.start_verification_sweep()
        with app.F.db() as c:
            c.execute("INSERT OR REPLACE INTO verification_sweep_attempts(sweep_id,key,started,outcome) VALUES (1,?,0,'queued')",(KEY,))
        from contextlib import ExitStack
        with ExitStack() as stack:
            for name in ('seed_validation','repair_cached_series'):
                stack.enter_context(patch.object(app.F,name))
            stack.enter_context(patch.object(app.REL,'schedule'))
            stack.enter_context(patch.object(app.F,'foreground_busy',return_value=False))
            stack.enter_context(patch.object(app.F,'jf',return_value=[]))
            stack.enter_context(patch.object(app.F,'first_sweep_candidate',return_value=((1,KEY),True)))
            stack.enter_context(patch.object(app.F,'metadata_admission_blocked',return_value=False))
            stack.enter_context(patch.object(app,'prepared',side_effect=app.SourceProviderWait('Source provider request deferred')))
            app.F.warm_availability(limit=1)
        with app.F.db() as c:
            self.assertEqual(tuple(c.execute('SELECT outcome,finished FROM verification_sweep_attempts WHERE key=?',(KEY,)).fetchone()),('queued',0))
            self.assertEqual(c.execute('SELECT count(*) FROM verification_claims').fetchone()[0],0)

class FallbackTests(unittest.TestCase):
    def setUp(self):
        fixtures.ImportTests.setUp(self)
        (app.DATA/'source-providers.json').write_text(json.dumps({'mediafusion_enabled':True,'mediafusion_url':'https://fallback.example.invalid'}))
    def tearDown(self):fixtures.ImportTests.tearDown(self)
    def stream(self,origin='BT4G'):
        return {'infoHash':'a'*40,'name':'MediaFusion 1080p','description':'x264 AAC\n🔗 '+origin,'behaviorHints':{'filename':'Example.Movie.2020.1080p.x264.mkv'}}
    def test_rate_limited_primary_uses_independent_fallback(self):
        import urllib.error
        def request(url,**kwargs):
            if url.startswith(app.PROVIDER):raise urllib.error.HTTPError(url,429,'limited',{},None)
            return {'streams':[self.stream(),self.stream()]}
        with patch.object(app,'fetch',side_effect=request):
            rows=app.stream_candidates('movie:tt0000001')
        self.assertEqual(len(rows),1);self.assertEqual(rows[0]['_provider'],'mediafusion')
    def test_cached_empty_primary_is_filled_without_primary_request(self):
        app.remember('streams:movie:tt0000001',[],300)
        app.remember('source-status:movie:tt0000001',{'state':'unavailable'},300)
        with patch.object(app,'fetch',return_value={'streams':[self.stream()]}) as fetch:
            self.assertEqual(len(app.stream_candidates('movie:tt0000001')),1)
            self.assertEqual(fetch.call_count,1)
            self.assertTrue(fetch.call_args.args[0].startswith('https://fallback.example.invalid/'))
            self.assertEqual(app.cached('source-status:movie:tt0000001')['state'],'listed')
    def test_torrentio_derived_and_unknown_origin_are_excluded(self):
        with patch.object(app,'fetch',return_value={'streams':[self.stream('Torrentio'),{**self.stream(),'description':'unknown'}]}):
            self.assertEqual(app.mediafusion_streams('movie:tt0000001'),[])
    def test_healthy_primary_does_not_query_fallback(self):
        with patch.object(app,'fetch',return_value={'streams':[{**self.stream(),'title':'Example 1080p x264'}]}) as fetch:
            self.assertEqual(len(app.stream_candidates('movie:tt0000001')),1)
            self.assertEqual(fetch.call_count,1)
    def test_two_provider_faults_do_not_cache_false_unavailable(self):
        import urllib.error
        with patch.object(app,'fetch',side_effect=urllib.error.HTTPError('https://example.invalid',429,'limited',{},None)):
            with self.assertRaises(app.Checkpoint):app.stream_candidates('movie:tt0000001')
        self.assertIsNone(app.cached('streams:movie:tt0000001'))
        self.assertIsNone(app.cached('source-status:movie:tt0000001'))
    def test_compatibility_filter_keeps_hevc_excluded_pending_tv_test(self):
        s=self.stream();s['title']='1080p HEVC x265'
        self.assertEqual(app.filtered_streams({'streams':[s]}),[])

    def test_malformed_responses_preserve_unknown_source_state(self):
        with patch.object(app,'fetch',side_effect=ValueError('Malformed upstream JSON')):
            with self.assertRaises(app.Checkpoint):app.stream_candidates('movie:tt0000001')
        self.assertIsNone(app.cached('source-status:movie:tt0000001'))
