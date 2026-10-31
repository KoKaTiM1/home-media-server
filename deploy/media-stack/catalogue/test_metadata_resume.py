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
