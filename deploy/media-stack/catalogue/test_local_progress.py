import http.client
from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
import threading,time,unittest
from unittest.mock import patch
from local_progress import Progress,ProgressProxy,response_offset

class ProgressTests(unittest.TestCase):
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
 def test_cancel_closes_only_owned_upstream_request(self):
  entered=threading.Event();release=threading.Event()
  class Handler(BaseHTTPRequestHandler):
   def log_message(self,*a):pass
   def do_GET(self):entered.set();release.wait(3)
  server=ThreadingHTTPServer(('127.0.0.1',0),Handler);server.daemon_threads=True;t=threading.Thread(target=server.serve_forever,daemon=True);t.start();proxy=ProgressProxy();url=proxy.source('http://127.0.0.1:'+str(server.server_port)+'/file',100)
  c=http.client.HTTPConnection('127.0.0.1',proxy.server.server_port,timeout=2)
  try:
   c.request('GET',url[len(proxy.prefix):]);self.assertTrue(entered.wait(2));proxy.operation([url]).stop.set();start=time.monotonic()
   with self.assertRaises((OSError,http.client.HTTPException)):c.getresponse()
   self.assertLess(time.monotonic()-start,1.5)
  finally:c.close();proxy.close();release.set();server.shutdown();server.server_close();t.join(timeout=2)
