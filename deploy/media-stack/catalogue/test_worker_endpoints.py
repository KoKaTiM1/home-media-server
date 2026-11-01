"""Use the last reachable pinned path until failure; do not probe a dead LAN."""
import hashlib,unittest
from unittest.mock import Mock,patch
from test_verification_worker import client as w

class EndpointTests(unittest.TestCase):
 def setUp(self):
  self.attempts=[];self.up={'primary.invalid':False,'fallback.invalid':True};self.cert={'primary.invalid':b'fixture-cert','fallback.invalid':b'fixture-cert'};self.pin=hashlib.sha256(b'fixture-cert').hexdigest();self.api=w.API({'server_url':'https://primary.invalid','fallback_server_urls':['https://fallback.invalid'],'tls_sha256':self.pin,'token':'x'*40})
 def connection(self,host,*args,**kwargs):
  c=Mock();c.host=host;c.sock.getpeercert.return_value=self.cert[host]
  def connect():
   self.attempts.append(host)
   if not self.up[host]:raise TimeoutError('fixture private endpoint')
  c.connect.side_effect=connect;return c
 def test_unreachable_primary_is_tried_once_then_fallback_sticks(self):
  with patch.object(w.http.client,'HTTPSConnection',side_effect=self.connection):
   self.assertEqual(self.api.connection().worker_endpoint,'fallback-1');self.assertEqual(self.api.connection().worker_endpoint,'fallback-1')
  self.assertEqual(self.attempts,['primary.invalid','fallback.invalid','fallback.invalid'])
 def test_recovered_lan_is_not_probed_while_fallback_is_healthy(self):
  with patch.object(w.http.client,'HTTPSConnection',side_effect=self.connection):
   self.api.connection();self.up['primary.invalid']=True
   for _ in range(3):self.assertEqual(self.api.connection().worker_endpoint,'fallback-1')
  self.assertEqual(self.attempts.count('primary.invalid'),1)
 def test_fallback_failure_switches_to_lan_and_stays_there(self):
  with patch.object(w.http.client,'HTTPSConnection',side_effect=self.connection):
   self.api.connection();self.up['primary.invalid']=True;self.up['fallback.invalid']=False
   self.assertEqual(self.api.connection().worker_endpoint,'primary');self.up['fallback.invalid']=True;self.assertEqual(self.api.connection().worker_endpoint,'primary')
  self.assertEqual(self.attempts,['primary.invalid','fallback.invalid','fallback.invalid','primary.invalid','primary.invalid'])
  self.assertEqual(self.api.origin.hostname,'primary.invalid')
 def test_healthy_lan_is_default_without_fallback_work(self):
  self.up['primary.invalid']=True
  with patch.object(w.http.client,'HTTPSConnection',side_effect=self.connection):
   self.assertEqual(self.api.connection().worker_endpoint,'primary');self.assertEqual(self.api.connection().worker_endpoint,'primary')
  self.assertEqual(self.attempts,['primary.invalid','primary.invalid'])
 def test_both_down_tries_each_once_and_preserves_transport_failure(self):
  self.up['fallback.invalid']=False
  with patch.object(w.http.client,'HTTPSConnection',side_effect=self.connection):
   with self.assertRaises(w.TransportWait):self.api.connection()
  self.assertEqual(len(self.attempts),2);self.assertIsNone(self.api.last_endpoint)
 def test_fallback_pin_mismatch_is_never_accepted(self):
  self.cert['fallback.invalid']=b'wrong-cert'
  with patch.object(w.http.client,'HTTPSConnection',side_effect=self.connection):
   with self.assertRaises(w.TransportWait):self.api.connection()
  self.assertIsNone(self.api.last_endpoint)
 def test_request_error_selects_other_path_on_next_attempt_without_replaying(self):
  c=self.connection('fallback.invalid');c.worker_origin_index=1;c.worker_endpoint='fallback-1';c.request.side_effect=OSError('secret');self.api.preferred_origin=1
  with patch.object(self.api,'connection',return_value=c):
   with self.assertRaises(w.TransportWait):self.api.request('/result',{'pending':'fixture'})
  self.assertEqual(self.api.preferred_origin,0);self.assertEqual(c.request.call_count,1);self.assertIsNone(self.api.last_endpoint)
 def test_http_auth_error_does_not_rotate_or_expose_body(self):
  c=self.connection('fallback.invalid');c.worker_origin_index=1;c.worker_endpoint='fallback-1';c.getresponse.return_value.status=401;c.getresponse.return_value.read.return_value=b'secret';self.api.preferred_origin=1
  with patch.object(self.api,'connection',return_value=c):
   with self.assertRaises(w.TransportWait) as e:self.api.request('/status')
  self.assertEqual(self.api.preferred_origin,1);self.assertNotIn('secret',str(e.exception))
