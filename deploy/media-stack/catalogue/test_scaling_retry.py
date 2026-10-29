import json,tempfile,time,unittest
from pathlib import Path
from unittest.mock import patch
from test_app import a
import features

class RetryTests(unittest.TestCase):
    def test_durable_backoff_excludes_audit_without_changing_proof(self):
        with tempfile.TemporaryDirectory() as d,patch.object(a,'DATA',Path(d)):
            key='movie:tt0000001';proof=json.dumps({'version':5,'checked_at':123})
            with a.F.db() as c:
                c.execute("INSERT INTO validation(key,state,proof) VALUES (?,'verified',?)",(key,proof))
            with a.REL.db() as c:
                c.execute('INSERT OR REPLACE INTO verification_audit(key,scheduled,priority,completed) VALUES (?,?,?,0)',(key,time.time(),100))
            now=time.time()
            for delay in (300,900,3600,21600,21600):
                with patch.object(features.time,'time',return_value=now):due=a.F.defer_verification(key,'transport')
                self.assertEqual(due,now+delay)
                self.assertIsNone(a.F.worker_audit())
            with a.F.db() as c:
                self.assertEqual(tuple(c.execute('SELECT state,proof FROM validation WHERE key=?',(key,)).fetchone()),('verified',proof))
            a.F.clear_verification_retry(key)
            self.assertEqual(a.F.worker_audit()[0],key)
            with patch.object(features.time,'time',return_value=now):self.assertEqual(a.F.defer_verification(key,'stream_stall'),now+300)
    def test_worker_helpers_preserve_identity_and_only_touch_markers(self):
        from test_verification_worker import client
        with tempfile.TemporaryDirectory() as d:
            root=Path(d);(root/'worker.sqlite').write_bytes(b'identity fixture')
            for command in ('request-drain','resume'):
                with patch('sys.argv',['worker',command,'--data',d]):client.main()
            self.assertFalse((root/'drain').exists());self.assertFalse((root/'drained').exists())
            self.assertEqual((root/'worker.sqlite').read_bytes(),b'identity fixture')

class TitleBudgetTests(unittest.TestCase):
    def setUp(self):
        from verification_jobs import DecodeJobs
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.clock=patch('verification_jobs.time.time',return_value=1000);self.now=self.clock.start();self.addCleanup(self.clock.stop)
        self.q=DecodeJobs(Path(self.tmp.name)/'queue.sqlite');self.q.initialize()
        self.q.set('local_torrent_enabled',True);self.q.set('local_torrent_permitted',True)
        self.q.claim('fixture-worker',['local-torrent-v1'])
        self.payload={'schema':1,'key':'movie:tt0000001','meta':{},'candidates':[{'infoHash':'a'*40}]}
    def submit(self):return self.q.submit(self.payload['key'],'a'*64,'verify_title',self.payload,'',480,execution_seconds=480)
    def test_late_claim_has_full_budget_and_renew_does_not_extend_it(self):
        job=self.submit();self.now.return_value=1479;self.q.poll(job)
        task=self.q.claim('fixture-worker',['local-torrent-v1'])
        self.assertEqual(task['deadline'],1959)
        self.now.return_value=1481;self.assertTrue(self.q.renew('fixture-worker',task))
        self.assertEqual(self.q.claim('fixture-worker',['local-torrent-v1'])['deadline'],1959)
        self.now.return_value=1959;self.assertFalse(self.q.renew('fixture-worker',task))
        self.assertEqual(self.q.poll(job)[0],'cancelled')
        self.q.finish(job,cancel_code='controller_budget')
        with self.q.db() as c:self.assertEqual(c.execute('SELECT cancel_code FROM decode_jobs').fetchone()[0],'execution_timeout')
    def test_queue_expiry_and_controller_loss_are_distinct(self):
        job=self.submit();self.now.return_value=1481;self.q.poll(job)
        self.assertIsNone(self.q.claim('fixture-worker',['local-torrent-v1']))
        self.q.finish(job)
        with self.q.db() as c:self.assertEqual(c.execute('SELECT cancel_code FROM decode_jobs WHERE id=?',(job,)).fetchone()[0],'queue_timeout')
        self.now.return_value=2000;self.q.seen('fixture-worker');job=self.submit();self.now.return_value=2031
        self.assertIsNone(self.q.claim('fixture-worker',['local-torrent-v1']))
        self.q.finish(job)
        with self.q.db() as c:self.assertEqual(c.execute('SELECT cancel_code FROM decode_jobs WHERE id=?',(job,)).fetchone()[0],'controller_lost')
    def test_diagnostics_survive_finish_and_reject_untrusted_values(self):
        import uuid
        job=self.submit();task=self.q.claim('fixture-worker',['local-torrent-v1'])
        result={'result_id':uuid.uuid4().hex,'status':'checkpoint','reason':'transport','diagnostic':{'code':'cache_attribution','category':'transport','failure_type':'Checkpoint','phase':'speed verification'},'metrics':{'lock_wait_seconds':12.5,'active_seconds':40}}
        self.assertEqual(self.q.receive('fixture-worker',task,result),'accepted');self.q.finish(job)
        with self.q.db() as c:
            r=c.execute('SELECT checkpoint_code,lock_wait_seconds,active_seconds,cancel_code,result FROM decode_jobs').fetchone()
            self.assertEqual(tuple(r),('cache_attribution',12.5,40,'',None))
        job=self.submit();task=self.q.claim('fixture-worker',['local-torrent-v1'])
        result['result_id']=uuid.uuid4().hex;result['diagnostic']['code']='secret-url';result['metrics']={'active_seconds':-1,'lock_wait_seconds':'private-text'}
        self.q.receive('fixture-worker',task,result);self.q.finish(job)
        with self.q.db() as c:self.assertEqual(tuple(c.execute('SELECT checkpoint_code,lock_wait_seconds,active_seconds FROM decode_jobs WHERE id=?',(job,)).fetchone()),('',0,0))
    def test_legacy_jobs_do_not_gain_more_time_at_claim(self):
        job=self.q.submit(self.payload['key'],'a'*64,'verify_title',self.payload,'',120)
        self.now.return_value=1020;self.q.poll(job)
        task=self.q.claim('fixture-worker',['local-torrent-v1']);self.assertEqual(task['deadline'],1120)
        self.q.initialize();self.assertEqual(self.q.claim('fixture-worker',['local-torrent-v1'])['deadline'],1120)
