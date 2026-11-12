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

    def test_background_windows_provider_slot_reports_wait_and_lookup_phases(self):
        app.BUDGET.validation_background=True
        app.BUDGET.remote_verification=True
        app.BUDGET.preparation_key=KEY
        app.BUDGET.remote_slot=3
        stages=[]
        with patch.object(app,'_report_provider_stage',side_effect=lambda control,phase:stages.append((control,phase))):
            with app.serialized_provider_slot(app.DATA/'test-provider.lock','jackett-yts-control') as timing:
                self.assertGreaterEqual(timing['queue_wait_ms'],0)
        self.assertEqual(stages,[('jackett-yts-control','provider slot wait'),
                                 ('jackett-yts-control','provider lookup')])
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

    def test_remote_first_sweep_preserves_420_second_v6_budget(self):
        from unittest.mock import Mock
        from contextlib import ExitStack
        app.F.start_verification_sweep()
        queue=Mock()
        queue.status.return_value={'windows_slots_online':1,'windows_capacity':8,
                                   'windows_queue_capacity':8,'totals':{'windows_batches_started':0}}
        queue.setting_read.side_effect=lambda name,default=None: True if name=='local_torrent_enabled' else default
        queue.local_workers.return_value=1
        observed={}
        def execute(*args,**kwargs):
            observed['remaining_seconds']=app.BUDGET.worker_deadline-time.monotonic()
            observed['first_sweep']=app.BUDGET.first_sweep_attempt
            return True
        with ExitStack() as stack:
            stack.enter_context(patch('verification_remote.ready',return_value=True))
            stack.enter_context(patch('verification_remote.jobs',return_value=queue))
            stack.enter_context(patch('verification_remote.producer_phase'))
            stack.enter_context(patch('verification_title.execute_title',side_effect=execute))
            stack.enter_context(patch.object(app.F,'seed_validation'))
            stack.enter_context(patch.object(app.REL,'schedule'))
            stack.enter_context(patch.object(app.F,'repair_cached_series'))
            stack.enter_context(patch.object(app.F,'next_verification_slot',return_value=1))
            stack.enter_context(patch.object(app.F,'first_sweep_candidate',return_value=((1,KEY),True)))
            stack.enter_context(patch.object(app.F,'metadata_admission_blocked',return_value=False))
            stack.enter_context(patch.object(app.F,'foreground_busy',return_value=False))
            stack.enter_context(patch.object(app.F,'jf',return_value=[]))
            stack.enter_context(patch.object(app,'metadata',return_value={'videos':[]}))
            app.F.warm_availability(limit=1,remote=True,slot=1)
        self.assertTrue(observed['first_sweep'])
        self.assertGreaterEqual(observed['remaining_seconds'],419)
        self.assertLessEqual(observed['remaining_seconds'],420)

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
    def setUp(self):
        fixtures.ImportTests.setUp(self)
        self.old_budget={name:getattr(app.BUDGET,name,None) for name in ('validation_background','remote_verification','worker_deadline','deadline','cancel')}
        app.BUDGET.validation_background=False;app.BUDGET.remote_verification=False
        app.BUDGET.worker_deadline=None;app.BUDGET.deadline=None;app.BUDGET.cancel=None
    def tearDown(self):
        for name,value in self.old_budget.items():
            if value is None:
                try:delattr(app.BUDGET,name)
                except AttributeError:pass
            else:setattr(app.BUDGET,name,value)
        fixtures.ImportTests.tearDown(self)
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

    def test_provider_slot_exposes_admission_wait_separately(self):
        with app.serialized_provider_slot(app.DATA/'timing-provider.lock','timing-provider-control') as timing:
            self.assertIsInstance(timing,dict)
            self.assertIsInstance(timing.get('queue_wait_ms'),int)
            self.assertGreaterEqual(timing['queue_wait_ms'],0)
            self.assertNotIn('http_ms',timing)
        with app.source_provider_slot() as primary_timing:
            self.assertIsInstance(primary_timing,dict)
            self.assertIsInstance(primary_timing.get('queue_wait_ms'),int)
            self.assertGreaterEqual(primary_timing['queue_wait_ms'],0)
    def test_background_remote_waits_for_shared_slot_and_five_second_spacing(self):
        import threading
        from contextlib import ExitStack
        held=threading.Event();release=threading.Event();entered=threading.Event();errors=[];clock=[1000.0]
        def owner():
            try:
                with app.source_provider_slot():held.set();release.wait(3)
            except Exception as error:errors.append(type(error).__name__)
        def remote():
            app.BUDGET.validation_background=True;app.BUDGET.remote_verification=True
            try:
                with app.source_provider_slot():entered.set()
            except Exception as error:errors.append(type(error).__name__)
        with ExitStack() as stack:
            stack.enter_context(patch.object(app.time,'time',side_effect=lambda:clock[0]))
            stack.enter_context(patch.object(app.time,'sleep',side_effect=lambda seconds:clock.__setitem__(0,clock[0]+seconds)))
            owner_thread=threading.Thread(target=owner);owner_thread.start();self.assertTrue(held.wait(2))
            remote_thread=threading.Thread(target=remote);remote_thread.start()
            # Let the remote lane observe the held lock before releasing it.
            import time as wall_time;wall_time.sleep(.05)
            self.assertFalse(entered.is_set())
            release.set()
            try:self.assertTrue(entered.wait(2))
            finally:owner_thread.join(3);remote_thread.join(3)
        self.assertEqual(errors,[])
        self.assertGreaterEqual(clock[0],1005)
    def test_background_provider_waiters_get_shared_slot_in_fifo_order(self):
        import threading,fcntl
        lock_path=app.DATA/'fifo-provider.lock';queue_dir=app.DATA/'fifo-provider.lock.waiters'
        held=threading.Event();release_owner=threading.Event();first_entered=threading.Event()
        release_first=threading.Event();second_entered=threading.Event();errors=[];order=[]
        def owner():
            try:
                with app.serialized_provider_slot(lock_path,'fifo-provider-control'):
                    held.set();release_owner.wait(5)
            except Exception as error:errors.append(type(error).__name__)
        def waiter(label,entered,release=None):
            app.BUDGET.validation_background=True
            try:
                with app.serialized_provider_slot(lock_path,'fifo-provider-control'):
                    order.append(label);entered.set()
                    if release is not None:release.wait(5)
            except Exception as error:errors.append(type(error).__name__)
        owner_thread=threading.Thread(target=owner);owner_thread.start()
        self.assertTrue(held.wait(2))
        try:
            first=threading.Thread(target=waiter,args=('first',first_entered,release_first));first.start()
            deadline=time.monotonic()+2
            while len(list(queue_dir.glob('*.ticket')))<1 and time.monotonic()<deadline:time.sleep(.01)
            self.assertEqual(len(list(queue_dir.glob('*.ticket'))),1)
            second=threading.Thread(target=waiter,args=('second',second_entered));second.start()
            deadline=time.monotonic()+2
            while len(list(queue_dir.glob('*.ticket')))<2 and time.monotonic()<deadline:time.sleep(.01)
            self.assertEqual(len(list(queue_dir.glob('*.ticket'))),2)
            release_owner.set()
            self.assertTrue(first_entered.wait(2))
            self.assertFalse(second_entered.is_set())
            release_first.set()
            self.assertTrue(second_entered.wait(2))
            first.join(2);second.join(2)
        finally:
            release_owner.set();release_first.set();owner_thread.join(2)
        self.assertEqual(order,['first','second'])
        self.assertEqual(errors,[])
        self.assertEqual(list(queue_dir.glob('*.ticket')),[])
    def test_speed_rejected_fallback_overtakes_normal_waiters_but_preserves_the_shared_slot(self):
        import threading
        lock_path=app.DATA/'priority-provider.lock';queue_dir=app.DATA/'priority-provider.lock.waiters'
        held=threading.Event();release_owner=threading.Event();normal_entered=threading.Event();priority_entered=threading.Event()
        order=[];errors=[]
        def owner():
            try:
                with app.serialized_provider_slot(lock_path,'priority-provider-control'):
                    held.set();release_owner.wait(5)
            except Exception as error:errors.append(type(error).__name__)
        def waiter(label,entered,priority=0):
            app.BUDGET.validation_background=True
            try:
                with app.serialized_provider_slot(lock_path,'priority-provider-control',priority=priority):
                    order.append(label);entered.set()
            except Exception as error:errors.append(type(error).__name__)
        owner_thread=threading.Thread(target=owner);owner_thread.start()
        self.assertTrue(held.wait(2))
        try:
            normal=threading.Thread(target=waiter,args=('normal',normal_entered));normal.start()
            deadline=time.monotonic()+2
            while len(list(queue_dir.glob('*.ticket')))<1 and time.monotonic()<deadline:time.sleep(.01)
            self.assertEqual(len(list(queue_dir.glob('*.ticket'))),1)
            priority=threading.Thread(target=waiter,args=('speed-retry',priority_entered,1));priority.start()
            deadline=time.monotonic()+2
            while len(list(queue_dir.glob('*.ticket')))<2 and time.monotonic()<deadline:time.sleep(.01)
            self.assertEqual(len(list(queue_dir.glob('*.ticket'))),2)
            release_owner.set()
            self.assertTrue(priority_entered.wait(2))
            self.assertTrue(normal_entered.wait(2))
            normal.join(2);priority.join(2)
        finally:
            release_owner.set();owner_thread.join(2)
        self.assertEqual(order,['speed-retry','normal'])
        self.assertEqual(errors,[])
        self.assertEqual(list(queue_dir.glob('*.ticket')),[])
    def test_speed_fallback_priority_alternates_with_waiting_normal_requests(self):
        import threading
        lock_path=app.DATA/'fair-priority-provider.lock';queue_dir=app.DATA/'fair-priority-provider.lock.waiters'
        held=threading.Event();release_owner=threading.Event();release_priority=threading.Event()
        normal_entered=threading.Event();priority_one_entered=threading.Event();priority_two_entered=threading.Event()
        order=[];errors=[]
        def owner():
            try:
                with app.serialized_provider_slot(lock_path,'fair-priority-provider-control'):
                    held.set();release_owner.wait(5)
            except Exception as error:errors.append(type(error).__name__)
        def waiter(label,entered,priority=0,release=None):
            app.BUDGET.validation_background=True
            try:
                with app.serialized_provider_slot(lock_path,'fair-priority-provider-control',priority=priority):
                    order.append(label);entered.set()
                    if release is not None:release.wait(5)
            except Exception as error:errors.append(type(error).__name__)
        owner_thread=threading.Thread(target=owner);owner_thread.start()
        self.assertTrue(held.wait(2))
        threads=[]
        try:
            for args in (('normal',normal_entered,0,None),('speed-retry-1',priority_one_entered,1,release_priority),
                         ('speed-retry-2',priority_two_entered,1,None)):
                thread=threading.Thread(target=waiter,args=args);thread.start();threads.append(thread)
                deadline=time.monotonic()+2
                while len(list(queue_dir.glob('*.ticket')))<len(threads) and time.monotonic()<deadline:time.sleep(.01)
                self.assertEqual(len(list(queue_dir.glob('*.ticket'))),len(threads))
            release_owner.set()
            self.assertTrue(priority_one_entered.wait(2))
            self.assertFalse(normal_entered.is_set())
            self.assertFalse(priority_two_entered.is_set())
            release_priority.set()
            self.assertTrue(normal_entered.wait(2))
            self.assertTrue(priority_two_entered.wait(2))
            for thread in threads:thread.join(2)
        finally:
            release_owner.set();release_priority.set();owner_thread.join(2)
            for thread in threads:thread.join(2)
        self.assertEqual(order,['speed-retry-1','normal','speed-retry-2'])
        self.assertEqual(errors,[])
        self.assertEqual(list(queue_dir.glob('*.ticket')),[])
    def test_background_provider_queue_reaps_ticket_left_by_dead_owner(self):
        import os
        lock_path=app.DATA/'orphan-provider.lock';queue_dir=app.DATA/'orphan-provider.lock.waiters'
        queue_dir.mkdir(parents=True)
        orphan=queue_dir/'00000000000000000000-dead-owner.ticket';orphan.touch()
        stale=time.time()-21*60;os.utime(orphan,(stale,stale))
        app.BUDGET.validation_background=True
        with app.serialized_provider_slot(lock_path,'orphan-provider-control'):
            self.assertFalse(orphan.exists())
        self.assertEqual(list(queue_dir.glob('*.ticket')),[])
    def test_provider_ticket_cleanup_distinguishes_restart_generation_and_legacy_grace(self):
        now=1000;generation=990
        self.assertTrue(app.provider_ticket_is_stale(980,'',now,generation))
        self.assertFalse(app.provider_ticket_is_stale(995,'',now,generation))
        self.assertTrue(app.provider_ticket_is_stale(999,'locked-v1',now,generation))
        self.assertTrue(app.provider_ticket_is_stale(100-1201,'',now,None))
        self.assertFalse(app.provider_ticket_is_stale(995,'',now,None))
    def test_background_source_wait_obeys_worker_deadline(self):
        app.BUDGET.validation_background=True;app.BUDGET.remote_verification=True
        app.BUDGET.worker_deadline=__import__('time').monotonic()-.01
        with (app.DATA/'source-provider.lock').open('a') as lock:
            import fcntl;fcntl.flock(lock,fcntl.LOCK_EX)
            try:
                with self.assertRaisesRegex(app.SourceProviderWait,'slot wait budget exhausted'):
                    with app.source_provider_slot():self.fail('Deadline should stop the wait')
            finally:fcntl.flock(lock,fcntl.LOCK_UN)
    def test_server_background_lane_uses_same_queue_but_not_remote_flag(self):
        clock=[1000.0]
        app.BUDGET.validation_background=True;app.BUDGET.remote_verification=False
        app.remember('source-provider-control',{'next':1005},90000)
        with patch.object(app.time,'time',side_effect=lambda:clock[0]),patch.object(app.time,'sleep',side_effect=lambda seconds:clock.__setitem__(0,clock[0]+seconds)):
            with app.source_provider_slot():self.assertGreaterEqual(clock[0],1005)
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
        other='movie:tt0000002'
        with app.F.db() as c:
            c.execute("INSERT INTO validation(key,state,priority) VALUES (?,'pending',1)",(other,))
            c.execute('UPDATE validation SET priority=100 WHERE key=?',(KEY,))
        app.F.start_verification_sweep()
        with app.F.db() as c:
            c.execute("UPDATE verification_sweep_attempts SET outcome='queued',started=0,finished=0 WHERE sweep_id=1 AND key=?",(KEY,))
        from contextlib import ExitStack
        with ExitStack() as stack:
            for name in ('seed_validation','repair_cached_series'):
                stack.enter_context(patch.object(app.F,name))
            stack.enter_context(patch.object(app.REL,'schedule'))
            stack.enter_context(patch.object(app.F,'foreground_busy',return_value=False))
            stack.enter_context(patch.object(app.F,'jf',return_value=[]))
            stack.enter_context(patch.object(app.F,'first_sweep_candidate',return_value=((1,KEY),True)))
            stack.enter_context(patch.object(app.F,'metadata_admission_blocked',return_value=False))
            stack.enter_context(patch.object(app,'prepared',side_effect=app.SourceProviderWait('Source provider request deferred',reason='queue_timeout')))
            app.F.warm_availability(limit=1)
        with app.F.db() as c:
            self.assertEqual(tuple(c.execute('SELECT outcome,finished FROM verification_sweep_attempts WHERE key=?',(KEY,)).fetchone()),('queued',0))
            self.assertEqual(c.execute('SELECT count(*) FROM verification_claims').fetchone()[0],0)
            wait=c.execute('SELECT expires FROM metadata_waits WHERE key=?',(KEY,)).fetchone()
        self.assertIsNotNone(wait)
        self.assertGreaterEqual(wait[0],time.time()+899)
        self.assertEqual(app.F.first_sweep_candidate()[0][1],other)

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
    def test_primary_empty_is_not_repeated_when_fallback_queue_times_out(self):
        key='movie:tt0000001'
        with patch.object(app,'fetch',return_value={'streams':[]}) as primary,\
             patch.object(app,'mediafusion_streams',side_effect=app.SourceProviderWait('queued')):
            with self.assertRaises(app.SourceProviderWait):app.stream_candidates(key)
            self.assertEqual(primary.call_count,1)
        self.assertEqual(app.cached('streams:'+key),[])
        with patch.object(app,'fetch',side_effect=AssertionError('repeat Torrentio lookup')) as fetch,\
             patch.object(app,'mediafusion_streams',return_value=[self.stream()]):
            rows=app.stream_candidates(key)
        self.assertEqual(len(rows),1)
        fetch.assert_not_called()
    def test_primary_transport_error_is_not_cached_as_an_empty_listing(self):
        import urllib.error
        key='movie:tt0000001'
        with patch.object(app,'fetch',side_effect=urllib.error.URLError('offline')),\
             patch.object(app,'mediafusion_streams',side_effect=app.SourceProviderWait('queued')):
            with self.assertRaises(app.SourceProviderWait):app.stream_candidates(key)
        self.assertIsNone(app.cached('streams:'+key))
    def test_torrentio_derived_and_unknown_origin_are_excluded(self):
        with patch.object(app,'fetch',return_value={'streams':[self.stream('Torrentio'),{**self.stream(),'description':'unknown'}]}):
            self.assertEqual(app.mediafusion_streams('movie:tt0000001'),[])
    def test_mediafusion_raw_origin_telemetry_is_aggregate_and_private(self):
        rows=[self.stream('Torrentio'),self.stream('BT4G'),self.stream('Other tracker'),
              {**self.stream(),'description':'no origin marker'},None,
              {**self.stream(),'description':'x264 AAC\n🔗   '}]
        counts=app.mediafusion_origin_counts(rows)
        self.assertEqual(counts,{'raw_rows':6,'malformed_rows':1,'torrentio_origin_rows':1,
            'named_origin_rows':2,'missing_or_unknown_origin_rows':2})
        with patch.object(app,'log') as logger:
            app.source_provider_event('mediafusion','empty',provider_rows=0,raw_stats=counts)
        event=logger.call_args.args[0]
        self.assertIn('"raw_rows":6',event)
        self.assertIn('"raw_torrentio_origin_rows":1',event)
        self.assertIn('"raw_named_origin_rows":2',event)
        self.assertIn('"raw_missing_or_unknown_origin_rows":2',event)
        self.assertIn('"raw_malformed_rows":1',event)
        self.assertNotIn('BT4G',event)
        self.assertNotIn('Torrentio',event)
        self.assertNotIn('tt0000001',event)
    def test_healthy_primary_does_not_query_fallback(self):
        with patch.object(app,'fetch',return_value={'streams':[{**self.stream(),'title':'Example 1080p x264'}]}) as fetch:
            rows=app.stream_candidates('movie:tt0000001')
            self.assertEqual(len(rows),1)
            self.assertEqual(rows[0]['_provider'],'torrentio')
            self.assertEqual(fetch.call_count,1)
    def test_speed_rejected_primary_adds_paced_fallback_only_on_background_retry(self):
        key='movie:tt0000001';primary={**self.stream(),'_provider':'torrentio'}
        fallback=[{**self.stream('BT4G'),'_provider':'mediafusion'},
                  {**self.stream('BT4G'),'infoHash':'b'*40,'_provider':'mediafusion'}]
        app.remember('streams:'+key,[primary],1800)
        app.remember('mediafusion-needed:'+key,True,21600)
        previous=getattr(app.BUDGET,'validation_background',False)
        app.BUDGET.validation_background=True
        try:
            with patch.object(app,'mediafusion_streams',return_value=fallback) as lookup,\
                 patch.object(app,'source_provider_event'):
                rows=app.stream_candidates(key)
            self.assertEqual({r['_provider'] for r in rows},{'torrentio','mediafusion'})
            self.assertEqual(len(rows),2) # Duplicate hashes are deduplicated.
            lookup.assert_called_once_with(key,priority=1)
            self.assertIsNone(app.cached('mediafusion-needed:'+key))
            with patch.object(app,'mediafusion_streams',side_effect=AssertionError('fallback repeated')):
                app.stream_candidates(key)
        finally:app.BUDGET.validation_background=previous
    def test_slow_source_marker_does_not_change_foreground_source_listing(self):
        key='movie:tt0000001';primary={**self.stream(),'_provider':'torrentio'}
        app.remember('streams:'+key,[primary],1800)
        app.remember('mediafusion-needed:'+key,True,21600)
        previous=getattr(app.BUDGET,'validation_background',False)
        app.BUDGET.validation_background=False
        try:
            with patch.object(app,'mediafusion_streams',side_effect=AssertionError('foreground lookup')):
                rows=app.stream_candidates(key)
            self.assertEqual([r['_provider'] for r in rows],['torrentio'])
            self.assertTrue(app.cached('mediafusion-needed:'+key))
        finally:app.BUDGET.validation_background=previous
    def test_fallback_queue_timeout_retains_retry_marker(self):
        key='movie:tt0000001';primary={**self.stream(),'_provider':'torrentio'}
        app.remember('streams:'+key,[primary],1800)
        app.remember('mediafusion-needed:'+key,True,21600)
        previous=getattr(app.BUDGET,'validation_background',False)
        app.BUDGET.validation_background=True
        try:
            with patch.object(app,'mediafusion_streams',side_effect=app.SourceProviderWait('queued',reason='queue_timeout')):
                with self.assertRaises(app.SourceProviderWait):app.stream_candidates(key)
            self.assertTrue(app.cached('mediafusion-needed:'+key))
        finally:app.BUDGET.validation_background=previous
    def test_remote_fallback_calls_queue_behind_the_shared_provider_slot(self):
        import threading,time as wall_time
        from contextlib import ExitStack
        real_sleep=wall_time.sleep
        clock=[1000.0];request_started=threading.Event();remote_waiting=threading.Event();release=threading.Event();remote_done=threading.Event();errors=[]
        def request(url,**kwargs):
            if 'tt0000001' in url:
                request_started.set();release.wait(3)
            return {'streams':[self.stream()]}
        def sleep(seconds):
            clock[0]+=seconds;remote_waiting.set();real_sleep(.001)
        def first():
            try:app.mediafusion_streams('movie:tt0000001')
            except Exception as error:errors.append(type(error).__name__)
        def second():
            app.BUDGET.validation_background=True;app.BUDGET.remote_verification=True
            try:app.mediafusion_streams('movie:tt0000002')
            except Exception as error:errors.append(type(error).__name__)
            finally:remote_done.set()
        with ExitStack() as stack:
            stack.enter_context(patch.object(app.time,'time',side_effect=lambda:clock[0]))
            stack.enter_context(patch.object(app.time,'sleep',side_effect=sleep))
            stack.enter_context(patch.object(app,'fetch',side_effect=request))
            first_thread=threading.Thread(target=first);first_thread.start();self.assertTrue(request_started.wait(2))
            second_thread=threading.Thread(target=second);second_thread.start();self.assertTrue(remote_waiting.wait(2))
            self.assertFalse(remote_done.is_set());release.set()
            try:self.assertTrue(remote_done.wait(2))
            finally:first_thread.join(3);second_thread.join(3)
        self.assertEqual(errors,[])
        self.assertGreaterEqual(clock[0],1015)
    def test_two_provider_faults_do_not_cache_false_unavailable(self):
        import urllib.error
        with patch.object(app,'fetch',side_effect=urllib.error.HTTPError('https://example.invalid',429,'limited',{},None)):
            with self.assertRaises(app.Checkpoint):app.stream_candidates('movie:tt0000001')
        self.assertIsNone(app.cached('streams:movie:tt0000001'))
        self.assertIsNone(app.cached('source-status:movie:tt0000001'))
    def test_fallback_upstream_failure_category_is_sanitized(self):
        import urllib.error
        def request(url,**kwargs):
            if url.startswith(app.PROVIDER):
                raise urllib.error.HTTPError(url,429,'limited',{},None)
            raise urllib.error.URLError('private upstream detail')
        events=[]
        with patch.object(app,'fetch',side_effect=request),patch.object(app,'log',side_effect=events.append):
            with self.assertRaises(app.Checkpoint):app.stream_candidates('movie:tt0000001')
        self.assertTrue(any('"provider":"mediafusion"' in event and '"error":"network"' in event for event in events))
        self.assertFalse(any('private upstream detail' in event or 'fallback.example.invalid' in event for event in events))
    def test_compatibility_filter_accepts_hevc_sdr_after_tv_test(self):
        s=self.stream();s['title']='1080p HEVC x265'
        self.assertEqual(len(app.filtered_streams({'streams':[s]})),1)
        for suffix in ('AV1','10bit','Main10','HDR10','Dolby Vision','DV','2160p'):
            self.assertEqual(app.filtered_streams({'streams':[{**s,'title':s['title']+' '+suffix}]}),[])

    def test_malformed_responses_preserve_unknown_source_state(self):
        with patch.object(app,'fetch',side_effect=ValueError('Malformed upstream JSON')):
            with self.assertRaises(app.Checkpoint):app.stream_candidates('movie:tt0000001')
        self.assertIsNone(app.cached('source-status:movie:tt0000001'))

class SourceDiagnosticsTests(unittest.TestCase):
    def setUp(self):fixtures.ImportTests.setUp(self)
    def tearDown(self):fixtures.ImportTests.tearDown(self)

    def test_filter_diagnostics_match_the_existing_candidate_decisions(self):
        accepted={'infoHash':'a'*40,'title':'Example 1080p x264'}
        rows=[accepted,{'infoHash':'bad','title':'Example 1080p'},
              {'infoHash':'b'*40,'title':'Example 1080p AV1'},
              {'infoHash':'c'*40,'title':'Example 1080p CAM'},
              {'infoHash':'d'*40,'title':'Example 480p'},
              {'infoHash':'e'*40,'title':'Example 720p','behaviorHints':{'filename':'Example.iso'}},
              {**accepted,'name':'duplicate'}]
        response={'streams':rows}
        regular=app.filtered_streams(response)
        diagnosed,stats=app.filter_streams_with_diagnostics(response)
        self.assertEqual(diagnosed,regular)
        self.assertEqual(stats,{'provider_rows':7,'invalid_infohash':1,'unsupported_release':1,
            'unsupported_codec':1,'unsupported_resolution':1,'unsupported_container':1,
            'duplicate_infohash':1,'compatible':1})

    def test_source_event_contains_only_safe_low_cardinality_fields(self):
        import urllib.error
        with patch.object(app.BUDGET,'validation_background',False,create=True), \
             patch.object(app.BUDGET,'remote_verification',False,create=True), \
             patch.object(app,'log') as logger:
            app.source_provider_event('torrentio','error',error=urllib.error.HTTPError(
                'https://private.example/tt0000001','429','limited',{},None))
        event=logger.call_args.args[0]
        self.assertIn('"error":"http_429"',event)
        self.assertIn('"scope":"interactive"',event)
        self.assertIn('"executor":"server"',event)
        self.assertNotIn('private.example',event)
        self.assertNotIn('tt0000001',event)

    def test_source_event_marks_background_remote_scope_without_private_fields(self):
        with patch.object(app.BUDGET,'validation_background',True,create=True), \
             patch.object(app.BUDGET,'remote_verification',True,create=True), \
             patch.object(app,'log') as logger:
            app.source_provider_event('mediafusion','empty',provider_rows=0)
        event=logger.call_args.args[0]
        self.assertIn('"scope":"background"',event)
        self.assertIn('"executor":"remote"',event)
        self.assertNotIn('tt0000001',event)

    def test_provider_timing_records_wait_and_http_separately_without_private_fields(self):
        error=app.SourceProviderWait('Provider queue full','private-control-key','capacity',30)
        error.queue_wait_ms=812
        with patch.object(app,'log') as logger:
            app.source_provider_event('jackett','error',error=error,indexer='yts')
        event=logger.call_args.args[0]
        self.assertIn('"queue_wait_ms":812',event)
        self.assertNotIn('"http_ms"',event)
        saved=app.cached('provider-timing:jackett:yts')
        self.assertEqual(saved['queue_wait_ms'],812)
        self.assertEqual(saved['result'],'wait_capacity')
        self.assertNotIn('private-control-key',str(saved));self.assertNotIn('tt0000001',str(saved))

    def test_provider_event_keeps_admitted_http_duration_separate_from_wait(self):
        with patch.object(app,'log') as logger:
            app.source_provider_event('mediafusion','empty',provider_rows=0,
                                      timing={'queue_wait_ms':2400,'http_ms':735})
        event=logger.call_args.args[0]
        self.assertIn('"queue_wait_ms":2400',event)
        self.assertIn('"http_ms":735',event)

    def test_torrentio_fetch_records_queue_and_http_time_without_changing_success_state(self):
        with patch.object(app,'fetch_raw',return_value={'streams':[]}) as request:
            result=app.fetch(app.PROVIDER+'/stream/movie/tt0000001.json')
        self.assertEqual(result,{'streams':[]});request.assert_called_once()
        timing=app.BUDGET.provider_timings['torrentio']
        self.assertIsInstance(timing['queue_wait_ms'],int)
        self.assertIsInstance(timing['http_ms'],int)
        self.assertEqual(app.cached('source-provider-control')['status'],200)
        with patch.object(app,'log') as logger:
            app.source_provider_event('torrentio','empty',provider_rows=0)
        event=logger.call_args.args[0]
        self.assertIn('"queue_wait_ms":',event);self.assertIn('"http_ms":',event)

    def test_source_diagnostic_logging_failure_does_not_interrupt_lookup(self):
        with patch.object(app,'log',side_effect=OSError('logger unavailable')):
            app.source_provider_event('mediafusion','empty',provider_rows=2,
                                      stats={'compatible':0})
