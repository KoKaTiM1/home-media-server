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
