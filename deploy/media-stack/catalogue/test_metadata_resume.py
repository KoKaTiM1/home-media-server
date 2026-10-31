"""Catalogue prerequisites resume untouched V6 work, never source retries."""
import json,time,unittest
from unittest.mock import patch
import test_metadata_lan as fixtures
app=fixtures.app
KEY='series:tt0000001:1:1'
class MetadataResumeTests(unittest.TestCase):
    setUp=fixtures.ImportTests.setUp
    tearDown=fixtures.ImportTests.tearDown
    submit=fixtures.ImportTests.submit
    def initialize(self):
        app.REL.db().close();app.F.start_verification_sweep()
    def wait(self,key=KEY,release=True):
        self.initialize();token=app.F.claim_verification(key,'fixture')
        self.assertTrue(app.F.mark_first_sweep_attempt(key))
        self.assertTrue(app.F.wait_for_metadata(key))
        app.F.finish_first_sweep_attempt(key,'deferred')
        if release:app.F.release_verification(key,token)
        return token
    def outcome(self,key=KEY):
        with app.F.db() as c:return c.execute('SELECT outcome FROM verification_sweep_attempts WHERE key=?',(key,)).fetchone()[0]
    def test_real_worker_wait_then_import_resumes_same_first_attempt(self):
        app.REL.db().close();app.BUDGET.metadata_background=True;self.jobs.seen()
        with patch.object(app.F,'seed_validation'),patch.object(app.REL,'schedule'),\
             patch.object(app.F,'repair_cached_series'),patch.object(app.F,'jf',return_value=[]),\
             patch.object(app,'prepared',side_effect=lambda *a,**k:app.metadata('series','tt0000001')):
            app.F.warm_availability(1)
        self.assertEqual(self.outcome(),'metadata_wait')
        self.submit();self.assertEqual(app.metadata_import(),1)
        self.assertEqual(self.outcome(),'queued')
        self.assertEqual(app.F.first_sweep_candidate()[0][1],KEY)
        with app.F.db() as c:
            state,attempts,proof=c.execute('SELECT state,attempts,proof FROM validation WHERE key=?',(KEY,)).fetchone()
        self.assertEqual((state,attempts),('pending',0));self.assertEqual(json.loads(proof),{'fixture':'original-proof'})
    def test_import_timer_fetches_waiting_dependency_without_metadata_helper(self):
        self.wait()
        with patch.object(app,'fetch',return_value={'meta':fixtures.result()['metadata']}) as fetch,\
             patch('metadata_jobs.MetadataJobs.seed'):
            app.metadata_import(seed=True)
        self.assertEqual(fetch.call_count,1)
        self.assertEqual(self.outcome(),'queued')
        with app.F.db() as c:
            row=c.execute('SELECT state,attempts,proof FROM validation WHERE key=?',(KEY,)).fetchone()
        self.assertEqual(row[:2],('pending',0));self.assertEqual(json.loads(row[2]),{'fixture':'original-proof'})

    def test_dependency_fetch_respects_provider_backoff_and_other_owner(self):
        self.wait();self.jobs.enqueue([('series','tt0000001',100)])
        task=self.jobs.claim('server','original','series','tt0000001')
        self.assertTrue(task)
        with self.jobs.db(True) as c:self.jobs.put(c,'provider_slot',0)
        with patch.object(app,'fetch',side_effect=AssertionError('duplicate fetch')):
            self.assertFalse(app.fetch_waiting_metadata(self.jobs))
        self.assertTrue(self.jobs.renew('server','original',task))
        with app.F.db() as c:c.execute('DELETE FROM verification_metadata_fetch')
        with self.jobs.db(True) as c:c.execute("UPDATE jobs SET state='pending',expires=0")
        self.jobs.provider_backoff(429)
        with patch.object(app,'fetch',side_effect=AssertionError('provider backoff')):
            self.assertFalse(app.fetch_waiting_metadata(self.jobs))
        self.assertEqual(self.outcome(),'metadata_wait')

    def test_dependency_fetch_failure_releases_own_lease_and_rotates(self):
        self.wait()
        with patch.object(app,'fetch',side_effect=TimeoutError):
            self.assertFalse(app.fetch_waiting_metadata(self.jobs))
        with self.jobs.db() as c:
            state,due=c.execute('SELECT state,due FROM jobs WHERE job_id=?',('series:tt0000001',)).fetchone()
        self.assertEqual(state,'retry');self.assertGreater(due,time.time())
        with patch.object(app,'fetch',side_effect=AssertionError('must back off')):
            self.assertFalse(app.fetch_waiting_metadata(self.jobs))
        self.assertEqual(self.outcome(),'metadata_wait')

    def test_dependency_fetch_skips_hold_and_source_retry(self):
        self.wait()
        for table,sql in [('hold','INSERT INTO verification_holds VALUES (?,?,?)'),
                          ('retry',None)]:
            if table=='hold':
                with app.F.db() as c:c.execute(sql,(KEY,time.time()+120,'fixture'))
            else:app.F.defer_verification(KEY,'transport')
            with patch.object(app,'fetch',side_effect=AssertionError('protected title')):
                self.assertFalse(app.fetch_waiting_metadata(self.jobs))
            with app.F.db() as c:c.execute('DELETE FROM verification_holds')

    def test_downloaded_state_never_resumes_even_with_same_checked(self):
        self.wait()
        with app.F.db() as c:c.execute("UPDATE validation SET state='downloaded' WHERE key=?",(KEY,))
        self.submit();app.metadata_import()
        self.assertEqual(self.outcome(),'deferred')
        with app.F.db() as c:self.assertEqual(c.execute('SELECT state FROM validation WHERE key=?',(KEY,)).fetchone()[0],'downloaded')

    def test_concurrent_dependency_fetch_has_one_provider_owner(self):
        self.wait()
        from concurrent.futures import ThreadPoolExecutor
        with patch.object(app,'fetch',return_value={'meta':fixtures.result()['metadata']}) as fetch:
            with ThreadPoolExecutor(max_workers=2) as pool:
                results=list(pool.map(lambda _:app.fetch_waiting_metadata(self.jobs),range(2)))
        self.assertEqual(sum(bool(x) for x in results),1);self.assertEqual(fetch.call_count,1)
        app.metadata_import();self.assertEqual(self.outcome(),'queued')

    def test_disabled_coordinator_leaves_dependency_waiting(self):
        self.wait();self.jobs.set_enabled(False)
        with patch.object(app,'fetch',side_effect=AssertionError('disabled coordinator')):
            self.assertFalse(app.fetch_waiting_metadata(self.jobs))
        self.assertEqual(self.outcome(),'metadata_wait')

    def test_previous_v5_proof_can_obtain_missing_metadata(self):
        with app.F.db() as c:c.execute("UPDATE validation SET state='verified',proof=? WHERE key=?",(json.dumps({'version':5}),KEY))
        self.wait()
        with patch.object(app,'fetch',return_value={'meta':fixtures.result()['metadata']}):
            self.assertTrue(app.fetch_waiting_metadata(self.jobs))
        app.metadata_import();self.assertEqual(self.outcome(),'queued')
        with app.F.db() as c:self.assertEqual(json.loads(c.execute('SELECT proof FROM validation').fetchone()[0]),{'version':5})

    def test_empty_dependency_queue_does_not_require_preparation_schema(self):
        with patch.object(app,'fetch',side_effect=AssertionError('no subscribed work')):
            self.assertFalse(app.fetch_waiting_metadata(self.jobs))

    def test_malformed_provider_metadata_returns_leased_retry_not_service_failure(self):
        self.wait()
        with patch.object(app,'fetch',return_value={'meta':{'id':'wrong'}}),\
             patch.object(self.jobs,'release',side_effect=AssertionError('use identity-bound receipt')):
            self.assertFalse(app.fetch_waiting_metadata(self.jobs))
        with self.jobs.db() as c:
            self.assertEqual(c.execute('SELECT state FROM jobs').fetchone()[0],'retry')
        self.assertEqual(app.metadata_import(),0);self.assertEqual(self.outcome(),'metadata_wait')

    def test_main_producer_invalid_metadata_releases_lease_and_waits(self):
        app.BUDGET.metadata_background=True
        with patch.object(app,'fetch',return_value={'meta':{'id':'wrong'}}):
            with self.assertRaises(app.MetadataPending):app.metadata('series','tt0000001')
        with self.jobs.db() as c:
            self.assertEqual(c.execute('SELECT state FROM jobs').fetchone()[0],'retry')
        with app.F.db() as c:
            self.assertEqual(c.execute('SELECT state,attempts FROM validation').fetchone(),('pending',0))

    def test_bounded_batch_imports_each_parent_before_selecting_next(self):
        keys=['movie:tt000000'+str(i) for i in range(2,7)]
        with app.F.db() as c:
            for key in keys:c.execute("INSERT INTO validation(key,state) VALUES (?,'pending')",(key,))
        for key in keys:self.wait(key)
        def fetched(url,**kwargs):
            imdb=url.rsplit('/',1)[1].split('.')[0]
            return {'meta':fixtures.result(kind='movie',imdb=imdb)['metadata']}
        def spacing(seconds):
            with self.jobs.db(True) as c:self.jobs.put(c,'provider_slot',0)
        with patch.object(app,'fetch',side_effect=fetched) as fetch,\
             patch.object(app.time,'sleep',side_effect=spacing),patch('metadata_jobs.MetadataJobs.seed'):
            applied=app.metadata_import(seed=True)
        self.assertEqual(applied,4);self.assertEqual(fetch.call_count,4)
        self.assertEqual(sum(self.outcome(k)=='queued' for k in keys),4)
        self.assertEqual(sum(self.outcome(k)=='metadata_wait' for k in keys),1)

    def test_provider_slot_contention_does_not_abort_whole_batch(self):
        with patch.object(app,'fetch_waiting_metadata',side_effect=[False,True,None]) as fetch,\
             patch.object(app.time,'sleep'),patch('metadata_jobs.MetadataJobs.seed'):
            app.metadata_import(seed=True)
        self.assertEqual(fetch.call_count,3)

    def test_claim_and_hold_block_resume_then_poll_recovers(self):
        token=self.wait(release=False);self.submit();app.metadata_import()
        self.assertEqual(self.outcome(),'metadata_wait')
        app.F.release_verification(KEY,token)
        with app.F.db() as c:
            c.execute('INSERT INTO verification_holds VALUES (?,?,?)',(KEY,time.time()+60,'fixture'))
            c.execute('UPDATE verification_metadata_dependencies SET next_check=0')
        app.F.resume_metadata_dependencies();self.assertEqual(self.outcome(),'metadata_wait')
        with app.F.db() as c:
            c.execute('DELETE FROM verification_holds');c.execute('UPDATE verification_metadata_dependencies SET next_check=0')
        self.assertEqual(app.F.resume_metadata_dependencies(),1);self.assertEqual(self.outcome(),'queued')
        first=app.F.claim_verification(KEY,'one');self.assertTrue(first)
        self.assertIsNone(app.F.claim_verification(KEY,'two'));app.F.release_verification(KEY,first)
    def test_real_source_retry_and_unmarked_history_remain_untouched(self):
        self.initialize();app.F.mark_first_sweep_attempt(KEY);app.F.finish_first_sweep_attempt(KEY,'deferred')
        app.F.defer_verification(KEY,'stream_stall')
        with app.F.db() as c:before=tuple(c.execute('SELECT * FROM verification_retry_waits WHERE key=?',(KEY,)).fetchone())
        self.submit();app.metadata_import();self.assertEqual(self.outcome(),'deferred')
        with app.F.db() as c:
            self.assertEqual(tuple(c.execute('SELECT * FROM verification_retry_waits WHERE key=?',(KEY,)).fetchone()),before)
            self.assertGreater(c.execute('SELECT expires FROM metadata_waits WHERE key=?',(KEY,)).fetchone()[0],time.time())
    def test_import_replay_is_idempotent_and_does_not_clear_source_cooldown(self):
        self.wait();app.F.defer_verification(KEY,'transport');self.submit()
        with patch.object(self.jobs,'imported',side_effect=OSError):
            # app resolves another coordinator instance; class patch covers it.
            from metadata_jobs import MetadataJobs
            with patch.object(MetadataJobs,'imported',side_effect=OSError):
                with self.assertRaises(OSError):app.metadata_import()
        app.metadata_import();self.assertEqual(self.outcome(),'metadata_wait')
        with app.F.db() as c:
            c.execute('DELETE FROM verification_retry_waits');c.execute('DELETE FROM metadata_waits');c.execute('UPDATE verification_metadata_dependencies SET next_check=0')
        self.assertEqual(app.F.resume_metadata_dependencies(),1)
        self.assertEqual(app.F.resume_metadata_dependencies(),0)
    def test_new_proof_does_not_get_reopened(self):
        self.wait()
        app.F.validation_result(KEY,True,proof={'version':6,'fixture':'new'})
        self.submit();app.metadata_import();self.assertNotEqual(self.outcome(),'queued')
        with app.F.db() as c:self.assertEqual(json.loads(c.execute('SELECT proof FROM validation WHERE key=?',(KEY,)).fetchone()[0]),{'version':6,'fixture':'new'})
    def test_cached_metadata_before_wait_marker_is_reconciled(self):
        self.submit();app.metadata_import();self.wait()
        self.assertEqual(app.F.resume_metadata_dependencies(),1)
        self.assertEqual(self.outcome(),'queued')
    def test_only_subscribed_episodes_resume_for_matching_parent(self):
        second='series:tt0000001:1:2';other='series:tt0000002:1:1'
        with app.F.db() as c:
            for key in [second,other]:c.execute("INSERT INTO validation(key,state) VALUES (?,'pending')",(key,))
        for key in [KEY,second,other]:self.wait(key)
        self.submit();app.metadata_import()
        self.assertEqual([self.outcome(k) for k in [KEY,second,other]],['queued','queued','metadata_wait'])

    def test_movie_import_preserves_probe_cooldown(self):
        key='movie:tt0000002'
        with app.F.db() as c:c.execute("INSERT INTO validation(key,state) VALUES (?,'pending')",(key,))
        self.wait(key)
        with app.F.db() as c:
            c.execute('INSERT INTO metadata_waits VALUES (?,?)',(key,time.time()+120))
            c.execute('INSERT INTO preparation_work VALUES (?,?,?)',(key,json.dumps({'speed_status':'cache_wait'}),time.time()))
        self.jobs.enqueue([('movie','tt0000002',100)])
        task=self.jobs.claim('server','server','movie','tt0000002')
        self.jobs.receive('server','server',task,fixtures.result(kind='movie',imdb='tt0000002'))
        app.metadata_import();self.assertEqual(self.outcome(key),'metadata_wait')
        with app.F.db() as c:self.assertGreater(c.execute('SELECT expires FROM metadata_waits WHERE key=?',(key,)).fetchone()[0],time.time())

    def test_expired_cache_waits_and_concurrent_reconciliation_resumes_once(self):
        self.wait();app.remember('meta:series:tt0000001',{'id':'tt0000001'},-1)
        self.assertEqual(app.F.resume_metadata_dependencies(),0)
        app.remember('meta:series:tt0000001',{'id':'tt0000001'},300)
        with app.F.db() as c:c.execute('UPDATE verification_metadata_dependencies SET next_check=0')
        from concurrent.futures import ThreadPoolExecutor
        with ThreadPoolExecutor(max_workers=2) as pool:results=list(pool.map(lambda _:app.F.resume_metadata_dependencies(),range(2)))
        self.assertEqual(sum(results),1);self.assertEqual(self.outcome(),'queued')

    def test_new_arrival_after_snapshot_completion_resumes_and_can_be_marked(self):
        self.initialize();app.F.mark_first_sweep_attempt(KEY);app.F.finish_first_sweep_attempt(KEY,'failed')
        self.assertEqual(app.F.first_sweep_candidate(),(None,False))
        key='movie:tt0000002'
        with app.F.db() as c:c.execute("INSERT INTO validation(key,state) VALUES (?,'pending')",(key,))
        self.assertEqual(app.F.first_sweep_candidate()[0][1],key)
        self.wait(key);app.remember('meta:movie:tt0000002',{'id':'tt0000002'},300)
        self.assertEqual(app.F.resume_metadata_dependencies(),1)
        self.assertEqual(app.F.first_sweep_candidate()[0][1],key)
        self.assertTrue(app.F.mark_first_sweep_attempt(key))
        self.assertFalse(app.F.mark_first_sweep_attempt(key))
        self.assertEqual(app.F.first_sweep_progress()['metadata_wait'],0)

    def test_real_checkpoint_after_resume_is_not_requeued_by_later_import(self):
        self.wait();self.submit();app.metadata_import()
        self.assertTrue(app.F.mark_first_sweep_attempt(KEY))
        app.F.finish_first_sweep_attempt(KEY,'deferred');app.F.defer_verification(KEY,'stream_stall')
        self.assertEqual(app.F.resume_metadata_dependencies(),0);self.assertEqual(self.outcome(),'deferred')
        with app.F.db() as c:self.assertEqual(c.execute('SELECT state FROM verification_metadata_dependencies').fetchone()[0],'finished')

    def test_new_manual_recheck_is_not_bypassed(self):
        self.wait()
        with app.F.db() as c:c.execute('INSERT INTO manual_rechecks VALUES (?,?,?,?)',(KEY,time.time()+1,'fixture','pending'))
        self.submit();app.metadata_import();self.assertEqual(self.outcome(),'metadata_wait')

    def test_waiters_keep_sweep_incomplete_without_hot_polling(self):
        self.wait();self.assertEqual(app.F.first_sweep_candidate(),(None,True))
        with app.F.db() as c:
            self.assertGreater(c.execute('SELECT next_check FROM verification_metadata_dependencies').fetchone()[0],time.time())
            self.assertEqual(c.execute('SELECT completed FROM verification_sweeps').fetchone()[0],0)
        p=app.F.first_sweep_progress();self.assertEqual(p['metadata_wait'],1);self.assertEqual(p['attempted'],0)
if __name__=='__main__':unittest.main()
