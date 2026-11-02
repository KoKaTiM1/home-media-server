"""Durable receipts survive retries, cleanup and process restart without inflation."""
import json,sqlite3,tempfile,time,unittest,uuid
from contextlib import closing
from pathlib import Path
from unittest.mock import patch
from verification_jobs import DecodeJobs
from verification_diagnostics import DiagnosticLedger,classify_error

class DiagnosticsTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup);self.root=Path(self.tmp.name);self.q=DecodeJobs(self.root/'queue.sqlite');self.q.initialize();self.q.set('enabled',True);self.q.seen('fixture',client_version='0.4.8');self.ledger=DiagnosticLedger(self.root/'verification-diagnostics.sqlite')
    def finish(self):
        ident=self.q.submit('movie:tt0000001','a'*64,'probe',{},'http://localhost/raw/movie:tt0000001',120);task=self.q.claim('fixture');self.q.renew('fixture',task,'video probe');self.q.receive('fixture',task,{'result_id':uuid.uuid4().hex,'status':'checkpoint','reason':'transport','diagnostic':{'category':'coordinator','failure_type':'OSError','phase':'video probe','code':'worker_permission'}});self.q.finish(ident);return ident
    def test_final_receipt_survives_payload_cleanup_and_export_failure(self):
        ident=self.finish()
        with self.q.db() as c:
            self.assertIsNone(c.execute('select result from decode_jobs where id=?',(ident,)).fetchone()[0]);self.assertEqual(c.execute('select count(*) from diagnostic_outbox').fetchone()[0],1)
        with patch.object(DiagnosticLedger,'ingest',side_effect=sqlite3.OperationalError('locked')):
            with self.assertRaises(sqlite3.OperationalError):self.q.flush_diagnostics()
        self.assertEqual(self.q.flush_diagnostics(),1)
        with closing(self.ledger.connect()) as c:
            raw=c.execute('select payload from latest').fetchone()[0];e=json.loads(raw);self.assertEqual(e['code'],'worker_permission');self.assertEqual(e['worker_version'],'0.4.8');self.assertNotIn('localhost',raw);self.assertIn('video probe',e['phase_seconds'])
    def test_commit_then_crash_replay_does_not_inflate_counts(self):
        self.finish()
        with self.q.db() as c:events=[tuple(r) for r in c.execute('select id,payload from diagnostic_outbox')]
        self.ledger.ingest(events);self.q.flush_diagnostics()
        with closing(self.ledger.connect()) as c:self.assertEqual(c.execute('select sum(count) from reason_counts').fetchone()[0],1)
    def test_three_details_retained_but_all_receipts_and_counts_survive(self):
        for _ in range(5):self.finish();self.q.flush_diagnostics()
        with closing(self.ledger.connect()) as c:
            self.assertEqual(c.execute('select count(*) from attempts').fetchone()[0],3);self.assertEqual(c.execute('select count(*) from receipts').fetchone()[0],5);self.assertEqual(c.execute('select sum(count) from reason_counts').fetchone()[0],5);self.assertEqual(c.execute('select sum(count) from buckets').fetchone()[0],1)
    def test_failure_bucket_is_replaced_when_later_attempt_succeeds(self):
        self.finish();self.q.flush_diagnostics()
        e={'scope':'worker_job','key':'movie:tt0000001','finished':time.time()+1,'outcome':'ok','category':'','code':''};self.ledger.ingest([('later',json.dumps(e))])
        with closing(self.ledger.connect()) as c:self.assertEqual(c.execute("select count from buckets where bucket='worker_job/checkpoint/coordinator/worker_permission'").fetchone()[0],0)
    def test_worker_and_sweep_outcomes_are_separate(self):
        for scope in ('worker_job','first_sweep'):
            self.ledger.ingest([(scope,json.dumps({'scope':scope,'key':'movie:tt0000001','finished':time.time(),'outcome':'deferred','category':'transport','code':'timeout'}))])
        with closing(self.ledger.connect()) as c:self.assertEqual(c.execute('select count(*) from latest').fetchone()[0],2)
    def test_error_classification_does_not_copy_secret_message(self):
        r=classify_error(RuntimeError('https://private.invalid/?token=secret'));self.assertEqual(r['code'],'unknown');self.assertNotIn('secret',str(r))

import test_metadata_lan as metadata_fixtures
class SweepReceiptTests(unittest.TestCase):
    setUp=metadata_fixtures.ImportTests.setUp
    tearDown=metadata_fixtures.ImportTests.tearDown
    def test_terminal_first_attempt_and_receipt_commit_together(self):
        a=metadata_fixtures.app;key='series:tt0000001:1:1';a.REL.db().close();a.F.start_verification_sweep();self.assertTrue(a.F.mark_first_sweep_attempt(key))
        a.F.finish_first_sweep_attempt(key,'deferred',{'category':'speed_rejected','code':'source_too_slow','failure_type':'SourceTooSlow'},time.time()-5)
        with a.F.db() as c:
            self.assertEqual(c.execute('select outcome from verification_sweep_attempts where key=?',(key,)).fetchone()[0],'deferred');e=json.loads(c.execute('select payload from verification_attempt_outbox').fetchone()[0]);self.assertEqual(e['scope'],'first_sweep');self.assertEqual(e['code'],'source_too_slow')
        a.F.finish_first_sweep_attempt(key,'deferred')
        with a.F.db() as c:self.assertEqual(c.execute('select count(*) from verification_attempt_outbox').fetchone()[0],1)
    def test_metadata_wait_is_not_a_completed_sweep_attempt(self):
        a=metadata_fixtures.app;key='series:tt0000001:1:1';a.REL.db().close();a.F.start_verification_sweep();a.F.mark_first_sweep_attempt(key);a.F.wait_for_metadata(key)
        a.F.finish_first_sweep_attempt(key,'deferred',{'category':'metadata_wait','code':'metadata_pending','failure_type':'MetadataPending'},time.time())
        with a.F.db() as c:
            self.assertEqual(c.execute('select outcome from verification_sweep_attempts where key=?',(key,)).fetchone()[0],'metadata_wait');self.assertEqual(json.loads(c.execute('select payload from verification_attempt_outbox').fetchone()[0])['scope'],'preparation')

class PhaseMetricTests(DiagnosticsTests):
    def test_phase_metrics_are_allowlisted_and_no_payload_text_is_saved(self):
        for phases,expected in [({'video probe':1.25},{'video probe':1.25}),({'private-secret':1},{}),({'video probe':961},{})]:
            ident=self.q.submit('movie:tt0000001','a'*64,'probe',{},'http://localhost/raw/movie:tt0000001',120);task=self.q.claim('fixture');self.q.receive('fixture',task,{'result_id':uuid.uuid4().hex,'status':'checkpoint','metrics':{'phase_seconds':phases}})
            with self.q.db() as c:self.assertEqual(json.loads(c.execute('select worker_phase_seconds from decode_jobs where id=?',(ident,)).fetchone()[0]),expected)
            self.q.finish(ident)

class ProofReceiptTests(DiagnosticsTests):
    def test_worker_ok_without_server_commit_is_not_an_accepted_title(self):
        ident=self.q.submit('movie:tt0000001','a'*64,'probe',{},'http://localhost/raw/movie:tt0000001',120);task=self.q.claim('fixture');self.q.receive('fixture',task,{'result_id':uuid.uuid4().hex,'status':'ok'});self.q.finish(ident,False);self.q.flush_diagnostics()
        with closing(self.ledger.connect()) as c:self.assertEqual(c.execute('select outcome from receipts').fetchone()[0],'unaccepted_ok')

class ExistingQueuePermissionTests(DiagnosticsTests):
    def test_initialize_does_not_chmod_an_already_shared_queue(self):
        with patch.object(Path,'chmod',side_effect=PermissionError('not owner')):
            self.q.initialize()

class InfrastructureSweepTests(unittest.TestCase):
    setUp=metadata_fixtures.ImportTests.setUp
    tearDown=metadata_fixtures.ImportTests.tearDown
    def run_interrupted_attempt(self,error):
        a=metadata_fixtures.app;a.REL.db().close()
        with patch.object(a.F,'seed_validation'),patch.object(a.REL,'schedule'),patch.object(a.F,'repair_cached_series'),patch.object(a.F,'jf',return_value=[]),patch.object(a,'prepared',side_effect=error),patch('features.time.sleep'):
            a.F.warm_availability(1)
        return a
    def test_local_runtime_failure_requeues_first_attempt_and_preserves_proof(self):
        from verification_title import LocalTitleCheckpoint
        a=self.run_interrupted_attempt(LocalTitleCheckpoint('coordinator','worker_permission'));key='series:tt0000001:1:1'
        with a.F.db() as c:
            r=c.execute('select outcome,finished,due from verification_sweep_attempts where key=?',(key,)).fetchone();self.assertEqual(r[0:2],('queued',0));self.assertGreater(r[2],time.time())
            self.assertEqual(c.execute('select attempts from validation where key=?',(key,)).fetchone()[0],0)
            self.assertEqual(json.loads(c.execute('select payload from verification_attempt_outbox').fetchone()[0])['scope'],'preparation')
    def test_busy_fallback_finishes_sweep_attempt_with_durable_retry(self):
        from verification_title import LocalTitleCheckpoint
        a=self.run_interrupted_attempt(LocalTitleCheckpoint('coordinator','preparation_lock_busy'))
        with a.F.db() as c:
            self.assertEqual(c.execute('select outcome from verification_sweep_attempts').fetchone()[0],'deferred')
            event=json.loads(c.execute('select payload from verification_attempt_outbox').fetchone()[0])
            self.assertEqual((event['scope'],event['code']),('first_sweep','preparation_lock_busy'))
            self.assertGreater(c.execute('select next_retry from verification_retry_waits').fetchone()[0],time.time())
            self.assertEqual(c.execute('select attempts from validation').fetchone()[0],0)

    def test_actual_speed_rejection_finishes_first_attempt_as_deferred(self):
        from verification_title import LocalTitleCheckpoint
        a=self.run_interrupted_attempt(LocalTitleCheckpoint('speed_rejected','speed_throughput_low'))
        with a.F.db() as c:
            self.assertEqual(c.execute('select outcome from verification_sweep_attempts').fetchone()[0],'deferred');self.assertEqual(json.loads(c.execute('select payload from verification_attempt_outbox').fetchone()[0])['scope'],'first_sweep')
    def test_database_fault_does_not_finish_the_first_attempt(self):
        e=sqlite3.OperationalError('private');e.sqlite_errorcode=5;a=self.run_interrupted_attempt(e)
        with a.F.db() as c:self.assertEqual(c.execute('select outcome from verification_sweep_attempts').fetchone()[0],'queued')

    def test_navigation_reason_is_retained_without_completing_first_attempt(self):
        a=metadata_fixtures.app
        def interrupted(*args,**kwargs):
            a.BUDGET.remote_cancel_code='navigation_interrupted'
            raise a.ClientCancelled('private client detail')
        self.run_interrupted_attempt(interrupted)
        with a.F.db() as c:
            self.assertEqual(c.execute('select outcome from verification_sweep_attempts').fetchone()[0],'queued')
            e=json.loads(c.execute('select payload from verification_attempt_outbox').fetchone()[0]);self.assertEqual(e['code'],'navigation_interrupted');self.assertNotIn('private client detail',str(e))
