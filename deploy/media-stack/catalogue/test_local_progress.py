import http.client
from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
import json
import os
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from local_progress import (LIFECYCLE_LIMIT,LIFECYCLE_TTL,LifecycleRecorder,
                            Progress,ProgressProxy,prune_lifecycle_artifacts,
                            response_offset,worker_lifecycle_paths)

class ProgressTests(unittest.TestCase):
 def test_job_state_resolves_lifecycle_marker_at_worker_data_root(self):
  with tempfile.TemporaryDirectory() as directory:
   root=Path(directory);state=root/'title-jobs'/'job-id'/'state'
   trace,marker=worker_lifecycle_paths(state)
   self.assertEqual(trace,root/'proxy-lifecycle.jsonl')
   self.assertEqual(marker,root/'proxy-lifecycle-observe-until')
   marker.write_text(str(time.time()+300));recorder=LifecycleRecorder(trace,marker,'trace-path-test')
   recorder.emit('proxy_start')
   self.assertEqual(json.loads(trace.read_text())['event'],'proxy_start')

 def test_unique_ranges_only_reset_watchdog(self):
  p=Progress('http://fixture.invalid/file',100)
  with patch('local_progress.time.monotonic',return_value=10):p.delivered(0,20)
  with patch('local_progress.time.monotonic',return_value=30):p.delivered(0,20)
  self.assertEqual(p.bytes,20);self.assertEqual(p.last,10)
  with patch('local_progress.time.monotonic',return_value=55):self.assertTrue(p.stalled())
  with patch('local_progress.time.monotonic',return_value=56):p.delivered(10,30);self.assertFalse(p.stalled())
  self.assertEqual(p.bytes,30)

 def test_reader_progress_is_isolated(self):
  p=Progress('http://fixture.invalid/file',100);q=Progress('http://fixture.invalid/file',100)
  with patch('local_progress.time.monotonic',return_value=10):p.last=q.last=10
  with patch('local_progress.time.monotonic',return_value=60):q.delivered(0,20);self.assertTrue(p.stalled());self.assertFalse(q.stalled())

 def test_range_response_must_match_exact_request_and_file(self):
  self.assertEqual(response_offset(206,'bytes 10-19/100','bytes=10-19',100),(10,20))
  for status,header,request in ((206,'bytes 11-19/100','bytes=10-19'),(206,'bytes 10-19/200','bytes=10-19'),(200,None,'bytes=10-19'),(206,'bytes 10-29/100','bytes=10-19')):
   with self.assertRaises(ValueError):response_offset(status,header,request,100)

 def test_real_http_ranges_and_duplicate_reads(self):
  class Handler(BaseHTTPRequestHandler):
   def log_message(self,*a):pass
   def do_GET(self):
    self.send_response(206);self.send_header('Content-Range','bytes 10-19/100');self.send_header('Content-Length','10');self.end_headers();self.wfile.write(b'0123456789')
  server=ThreadingHTTPServer(('127.0.0.1',0),Handler);t=threading.Thread(target=server.serve_forever,daemon=True);t.start();proxy=ProgressProxy()
  try:
   url=proxy.source('http://127.0.0.1:'+str(server.server_port)+'/file',100);operation=proxy.operation([url])
   for _ in range(2):
    c=http.client.HTTPConnection('127.0.0.1',proxy.server.server_port,timeout=3);c.request('GET',url[len(proxy.prefix):],headers={'Range':'bytes=10-19'});r=c.getresponse();self.assertEqual(r.status,206);self.assertEqual(r.read(),b'0123456789');c.close()
   self.assertEqual(operation.bytes,10)
  finally:proxy.close();server.shutdown();server.server_close();t.join(timeout=2)

 def test_cancel_trace_covers_stop_observation_and_close_without_private_fields(self):
  entered=threading.Event();release=threading.Event()
  class Handler(BaseHTTPRequestHandler):
   def log_message(self,*a):pass
   def do_GET(self):entered.set();release.wait(3)
  with tempfile.TemporaryDirectory() as directory:
   root=Path(directory);marker=root/'observe-until';marker.write_text(str(time.time()+300));trace=root/'trace.jsonl'
   server=ThreadingHTTPServer(('127.0.0.1',0),Handler);server.daemon_threads=True;t=threading.Thread(target=server.serve_forever,daemon=True);t.start();proxy=ProgressProxy(trace,marker)
   url=proxy.source('http://127.0.0.1:'+str(server.server_port)+'/private-path',100);c=http.client.HTTPConnection('127.0.0.1',proxy.server.server_port,timeout=2)
   try:
    c.request('GET',url[len(proxy.prefix):]);self.assertTrue(entered.wait(2));proxy.operation([url]).request_stop('verifier_command_finished')
    with self.assertRaises((OSError,http.client.HTTPException)):c.getresponse()
    release.set();proxy.close();deadline=time.monotonic()+2;events=[]
    while time.monotonic()<deadline:
     if trace.exists():events=[json.loads(line) for line in trace.read_text().splitlines()]
     if any(event['event']=='handler_close' for event in events):break
     time.sleep(.02)
    names=[event['event'] for event in events]
    self.assertIn('request_stop_signal',names);self.assertIn('handler_stop_observed',names);self.assertIn('handler_close',names)
    self.assertTrue(all(not ({'url','hash','title','path','ip'}&event.keys()) for event in events))
    self.assertEqual({event['trace'] for event in events},{proxy.lifecycle.trace_id})
   finally:c.close();release.set();proxy.close();server.shutdown();server.server_close();t.join(timeout=2)

 def test_opt_in_trace_records_upstream_status_and_last_byte_timing_only(self):
  class Handler(BaseHTTPRequestHandler):
   def log_message(self,*a):pass
   def do_GET(self):
    self.send_response(206);self.send_header('Content-Range','bytes 10-19/100');self.send_header('Content-Length','10');self.end_headers();self.wfile.write(b'0123456789')
  with tempfile.TemporaryDirectory() as directory:
   root=Path(directory);marker=root/'observe-until';marker.write_text(str(time.time()+300));trace=root/'trace.jsonl'
   server=ThreadingHTTPServer(('127.0.0.1',0),Handler);server.daemon_threads=True;t=threading.Thread(target=server.serve_forever,daemon=True);t.start();proxy=ProgressProxy(trace,marker)
   url=proxy.source('http://127.0.0.1:'+str(server.server_port)+'/private-hash-url',100)
   try:
    c=http.client.HTTPConnection('127.0.0.1',proxy.server.server_port,timeout=3);c.request('GET',url[len(proxy.prefix):],headers={'Range':'bytes=10-19'});response=c.getresponse()
    self.assertEqual(response.status,206);self.assertEqual(response.read(),b'0123456789');c.close();proxy.close()
    events=[json.loads(line) for line in trace.read_text().splitlines()]
    header=next(event for event in events if event['event']=='handler_response')
    closed=next(event for event in events if event['event']=='handler_close')
    self.assertEqual(header['response_status'],206);self.assertEqual(header['response_range_kind'],'partial');self.assertEqual(header['response_content_length'],10)
    self.assertEqual(closed['upstream_bytes'],10);self.assertEqual(closed['bytes_sent'],10)
    self.assertGreaterEqual(closed['last_byte_elapsed_ms'],0);self.assertGreaterEqual(closed['last_byte_age_ms'],0)
    self.assertEqual(closed['unique_bytes'],10);self.assertGreaterEqual(closed['unique_progress_age_ms'],0)
    self.assertTrue(all(not ({'url','hash','title','path','ip','range'}&event.keys()) for event in events))
   finally:proxy.close();server.shutdown();server.server_close();t.join(timeout=2)

 def test_lifecycle_is_opt_in_allowlisted_and_size_bounded(self):
  with tempfile.TemporaryDirectory() as directory:
   root=Path(directory);marker=root/'observe-until';marker.write_text(str(time.time()+300));path=root/'trace.jsonl'
   recorder=LifecycleRecorder(path,marker,'trace-test')
   recorder.emit('handler_start',request_id='req-a',method='GET',title='private title',url='http://secret.invalid/x',source_hash='a'*40)
   row=json.loads(path.read_text());self.assertEqual(row['trace'],'trace-test');self.assertEqual(row['event'],'handler_start')
   self.assertEqual(row['request_id'],'req-a');self.assertEqual(row['method'],'GET')
   self.assertFalse({'title','url','source_hash'}&row.keys())
  with tempfile.TemporaryDirectory() as directory:
   root=Path(directory);marker=root/'observe-until';marker.write_text(str(time.time()+300));path=root/'trace.jsonl'
   recorder=LifecycleRecorder(path,marker,'trace-cap')
   with patch('local_progress.LIFECYCLE_LIMIT',300):
    for n in range(20):recorder.emit('handler_close',request_id=str(n),reason='response_complete',elapsed_ms=n,bytes_sent=n)
   self.assertLessEqual(path.stat().st_size,300)

 def test_observation_window_cannot_be_extended_past_one_hour(self):
  with tempfile.TemporaryDirectory() as directory:
   root=Path(directory);marker=root/'observe-until';marker.write_text(str(time.time()+3601));path=root/'trace.jsonl'
   recorder=LifecycleRecorder(path,marker,'trace-too-long');recorder.emit('proxy_start')
   self.assertFalse(path.exists())

 def test_expired_observation_and_log_retention_are_pruned(self):
  with tempfile.TemporaryDirectory() as directory:
   root=Path(directory);marker=root/'proxy-lifecycle-observe-until';trace=root/'proxy-lifecycle.jsonl'
   marker.write_text(str(time.time()-1));trace.write_text('{}\n');old=time.time()-LIFECYCLE_TTL-1;os.utime(trace,(old,old))
   prune_lifecycle_artifacts(root)
   self.assertFalse(marker.exists());self.assertFalse(trace.exists())

if __name__=='__main__':unittest.main()
