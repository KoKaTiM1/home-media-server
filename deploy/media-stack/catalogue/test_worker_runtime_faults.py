"""Local faults are acknowledged checkpoints; network failures retain work."""
import unittest
from unittest.mock import patch
from test_verification_worker import client

class RuntimeFaultTests(unittest.TestCase):
    def test_permission_fault_is_local_checkpoint_without_secret_text(self):
        with patch.object(client,'run_task',side_effect=PermissionError('https://private.invalid/?token=secret')):
            r=client.execute_task(None,None,{},lambda x:None)
        self.assertEqual(r['diagnostic']['code'],'worker_permission')
        self.assertEqual(r['diagnostic']['category'],'coordinator')
        self.assertEqual(r['status'],'checkpoint')
        self.assertNotIn('secret',str(r))
    def test_import_and_invalid_payload_are_separate(self):
        for e,code in [(ImportError('private'),'worker_import'),(ValueError('private'),'worker_task_invalid'),(OSError('private'),'worker_local_io')]:
            with self.subTest(code=code),patch.object(client,'run_task',side_effect=e):
                self.assertEqual(client.execute_task(None,None,{},lambda x:None)['diagnostic']['code'],code)
    def test_transport_failure_retains_current_task(self):
        with patch.object(client,'run_task',side_effect=client.TransportWait('request_timeout')):
            with self.assertRaises(client.TransportWait):client.execute_task(None,None,{},lambda x:None)
    def test_success_keeps_evidence_unchanged(self):
        evidence={'status':'ok','data':{'proof':'fixture'}}
        with patch.object(client,'run_task',return_value=evidence):
            self.assertIs(client.execute_task(None,None,{},lambda x:None),evidence)

    def test_http_failure_uses_endpoint_label_without_response_or_address(self):
        from unittest.mock import Mock
        api=client.API({'server_url':'https://private.invalid','token':'x'*40,'tls_sha256':'a'*64})
        c=Mock();c.worker_endpoint='fallback-1';c.getresponse.return_value.status=401;c.getresponse.return_value.read.return_value=b'private token secret'
        with patch.object(api,'connection',return_value=c):
            with self.assertRaises(client.TransportWait) as e:api.request('/status')
        self.assertEqual(str(e.exception),'http_401 endpoint=fallback-1');self.assertNotIn('secret',str(e.exception));c.close.assert_called_once()
    def test_request_io_error_is_transport_wait_and_not_local_checkpoint(self):
        from unittest.mock import Mock
        api=client.API({'server_url':'https://private.invalid','token':'x'*40,'tls_sha256':'a'*64});c=Mock();c.worker_endpoint='primary';c.request.side_effect=OSError('private token secret')
        with patch.object(api,'connection',return_value=c):
            with self.assertRaises(client.TransportWait) as e:api.request('/status')
        self.assertEqual(str(e.exception),'request_transport endpoint=primary')
