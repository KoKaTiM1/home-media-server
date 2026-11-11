"""Tracker handoff preserves identity and excludes arbitrary provider URLs."""
import copy,io,json,time,unittest,urllib.error
from contextlib import ExitStack
from urllib.parse import urlencode,parse_qs,urlsplit
from unittest.mock import patch
from source_discovery import tracker_sources,magnet_tracker_sources,indexer_error_details,PUBLIC_TRACKERS,DEFAULT_TRACKER,torznab_streams,failure_state
from verification_title import payload,validate_task,magnet
import test_verification_title as titlebase
import test_source_discovery as discoverybase
a=titlebase.a;KEY=titlebase.KEY;HASH=titlebase.HASH
EXTRA='udp://open.stealth.si:80/announce'
HINT='tracker:'+EXTRA
class TrackerTests(unittest.TestCase):
 def test_exact_allowlist_and_bounded_deduplication(self):
  good=['tracker:'+x for x in sorted(PUBLIC_TRACKERS)]
  bad=['tracker:http://127.0.0.1:8090/announce','tracker:http://[::1]/announce','tracker:https://private.invalid/announce','tracker:https://user:secret@tracker.gbitt.info:443/announce','tracker:https://tracker.gbitt.info:443/announce?token=secret','tracker:https://tracker.gbitt.info:443/announce#secret','tracker:udp://open.stealth.si:81/announce','tracker:udp://open.stealth.si:80/other','tracker:udp://open.stealth.si:80/announce/','tracker:udp://open.stealth.si:80/announce\n','dht:'+HASH,None,{},'x'*2000]
  self.assertEqual(tracker_sources(bad+good+good),good)
  self.assertEqual(len(tracker_sources(good*50)),8)
  for value in (None,'tracker:'+EXTRA,{},tuple(good)):self.assertEqual(tracker_sources(value),[])
 def test_magnet_trackers_only_keep_approved_hints_without_following_links(self):
  link='magnet:?'+urlencode([('xt','urn:btih:'+HASH),('tr',EXTRA),('tr','http://127.0.0.1/private'),('tr',EXTRA)])
  self.assertEqual(magnet_tracker_sources(link),[HINT])
  for link in ('http://private.invalid/secret',None,'magnet:?'+('tr=x&'*200),'magnet:?'+('x'*17000)):
   self.assertEqual(magnet_tracker_sources(link),[])
 def test_torznab_preserves_trackers_and_existing_imdb_guard(self):
  from xml.sax.saxutils import escape
  link='magnet:?'+urlencode([('xt','urn:btih:'+HASH),('tr',EXTRA),('tr','http://127.0.0.1/private')])
  raw=('<rss xmlns:t="http://torznab.com/schemas/2015/feed"><channel><item><title>Fixture.2026.1080p</title><link>'+escape(link)+'</link><t:attr name="imdbid" value="tt0000001"/></item></channel></rss>').encode()
  rows=torznab_streams(raw,KEY)
  self.assertEqual(rows[0]['infoHash'],HASH);self.assertEqual(rows[0]['sources'],[HINT])
  self.assertEqual(torznab_streams(raw,'movie:tt0000002'),[])
  self.assertNotIn('torrentUrl',rows[0]);self.assertNotIn('url',rows[0])
 def test_magnet_keeps_default_and_exact_hash_with_distinct_trackers(self):
  q=parse_qs(urlsplit(magnet(HASH,[HINT,'tracker:'+DEFAULT_TRACKER,HINT])).query)
  self.assertEqual(q,{'xt':['urn:btih:'+HASH],'tr':[DEFAULT_TRACKER,EXTRA]})

class HandoffTests(unittest.TestCase):
 def setUp(self):
  self.t=titlebase.TitleTests();self.t.setUp();self.addCleanup(self.t.doCleanups)
 def build(self,candidate):
  with patch.object(a,'metadata',return_value=self.t.meta),patch.object(a,'verification_candidates',return_value=[candidate]),patch.object(a,'fetch',side_effect=AssertionError('No provider request')):
   return payload(a.__dict__,KEY)
 def test_provider_tracker_hints_survive_windows_payload(self):
  candidate=copy.deepcopy(self.t.payload['candidates'][0]);candidate['sources']=[HINT,'tracker:http://127.0.0.1/private',HINT];candidate['_provider']='mediafusion'
  p=self.build(candidate);validate_task(p)
  self.assertEqual(p['candidates'][0].get('sources',[]),[HINT]);self.assertEqual(p['candidates'][0]['infoHash'],HASH)
  self.assertEqual(candidate['sources'],[HINT,'tracker:http://127.0.0.1/private',HINT])
 def test_exact_source_link_trackers_survive_safe_magnet_rebuild(self):
  candidate=copy.deepcopy(self.t.payload['candidates'][0]);candidate['_exact']={'hash':HASH,'index':3,'filename':'Fixture.2026.mp4','length':123456,'link':'magnet:?'+urlencode([('xt','urn:btih:'+HASH),('tr',EXTRA),('tr','https://private.invalid/secret')])}
  p=self.build(candidate);x=p['candidates'][0];q=parse_qs(urlsplit(x['_exact']['link']).query)
  self.assertEqual(x.get('sources',[]),[HINT]);self.assertEqual(q['tr'],[DEFAULT_TRACKER,EXTRA]);self.assertEqual(q['xt'],['urn:btih:'+HASH])
  self.assertEqual({k:x['_exact'][k] for k in ('hash','index','filename','length')},{k:candidate['_exact'][k] for k in ('hash','index','filename','length')})
 def test_task_validation_rejects_untrusted_or_unbounded_sources(self):
  for values in ([HINT,HINT],['tracker:http://127.0.0.1/private'],'tracker:'+EXTRA,[HINT]*100,{}):
   with self.subTest(values=str(values)[:30]):
    p=copy.deepcopy(self.t.payload);p['candidates'][0]['sources']=values
    with self.assertRaises(ValueError):validate_task(p)
  validate_task(self.t.payload)

class IndexerDiagnosticTests(unittest.TestCase):
 def test_only_fixed_labels_and_statuses_survive_body_inspection(self):
  secret=b' https://private.invalid/key?apikey=credential-secret'
  cases=[(b'<error description="Exception (thepiratebay): Status=TooManyRequests Response=429 Too Many Requests"/>','upstream_http_429'),(b'{"error":"Exception (yts): Connection reset by peer"}','connection_reset'),(secret,'unknown'),(b'x'*8193,'unknown')]
  for body,label in cases:
   details=indexer_error_details(400,body)
   self.assertEqual(details['indexer_failure'],label);self.assertEqual(details['adapter_http_status'],400)
   self.assertEqual(details.get('upstream_http_status'),429 if label=='upstream_http_429' else None)
   self.assertNotIn('private.invalid',json.dumps(details));self.assertNotIn('credential',json.dumps(details))
  self.assertEqual(indexer_error_details('secret',secret),{})
 def test_jackett_body_reason_is_logged_without_changing_actual_backoff(self):
  app=discoverybase.app
  fixture=discoverybase.ChainTests();fixture.setUp();self.addCleanup(fixture.tearDown);fixture.configure()
  error=urllib.error.HTTPError('http://jackett:9117/private?apikey=secret',400,'private-detail',{},io.BytesIO(b'<error description="Connection reset by peer https://private.invalid/secret"/>'))
  with patch.object(app,'fetch',side_effect=error),patch.object(app.time,'time',return_value=1000),patch.object(app.os,'urandom',return_value=b'\0\0'),patch.object(app,'log') as log:
   with self.assertRaises(app.SourceProviderWait):app.jackett_streams(KEY)
   state=app.cached('jackett-yts-control');expected=failure_state({},400,{},15,1000,0)
   self.assertEqual({k:v for k,v in state.items() if k not in ('adapter_http_status','indexer_failure','upstream_http_status')},expected)
   self.assertEqual(state['indexer_failure'],'connection_reset');self.assertEqual(error.code,400)
   lines=' '.join(str(x.args[0]) for x in log.call_args_list)
   self.assertIn('"adapter_http_status":400',lines);self.assertIn('connection_reset',lines)
   for secret in ('private.invalid','apikey','private-detail'):self.assertNotIn(secret,lines)
   app.provider_succeeded('jackett-yts-control',15);state=app.cached('jackett-yts-control')
   self.assertNotIn('indexer_failure',state);self.assertNotIn('adapter_http_status',state)
 def test_large_body_is_inspected_only_to_fixed_bound_and_closed(self):
  app=discoverybase.app
  fixture=discoverybase.ChainTests();fixture.setUp();self.addCleanup(fixture.tearDown);fixture.configure()
  class Bounded(io.BytesIO):
   requested=[]
   def read(self,n=-1):self.requested.append(n);return super().read(n)
  body=Bounded(b'x'*9000);error=urllib.error.HTTPError('http://jackett:9117/private',400,'private',{},body)
  with patch.object(app,'fetch',side_effect=error):
   with self.assertRaises(app.SourceProviderWait):app.jackett_streams(KEY)
  self.assertEqual(body.requested,[8193]);self.assertTrue(body.closed)
  self.assertEqual(error.indexer_details,{'adapter_http_status':400,'indexer_failure':'unknown'})
 def test_trickling_error_body_has_short_diagnostic_timeout(self):
  import threading,urllib.request
  from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
  app=discoverybase.app
  fixture=discoverybase.ChainTests();fixture.setUp();self.addCleanup(fixture.tearDown);fixture.configure()
  release=threading.Event()
  class Handler(BaseHTTPRequestHandler):
   def log_message(self,*args):pass
   def do_GET(self):
    self.send_response(400);self.send_header('Content-Length','9000');self.end_headers();self.wfile.flush()
    release.wait(3)
  server=ThreadingHTTPServer(('127.0.0.1',0),Handler);thread=threading.Thread(target=server.serve_forever);thread.start()
  try:
   try:urllib.request.urlopen('http://127.0.0.1:'+str(server.server_port)+'/',timeout=4)
   except urllib.error.HTTPError as caught:error=caught
   started=time.monotonic()
   with patch.object(app,'fetch',side_effect=error):
    with self.assertRaises(app.SourceProviderWait):app.jackett_streams(KEY)
   self.assertLess(time.monotonic()-started,1.8)
   self.assertEqual(error.code,400);self.assertEqual(error.indexer_details['indexer_failure'],'unknown')
  finally:release.set();server.shutdown();server.server_close();thread.join(2)
if __name__=='__main__':unittest.main()
