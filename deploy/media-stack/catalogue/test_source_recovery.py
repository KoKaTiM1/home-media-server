"""Provider recovery is a timed prerequisite, never a failed media proof."""
import json,time,unittest
from contextlib import ExitStack
from unittest.mock import patch
import test_preparation_pool as base
from test_source_discovery import CAPS,listing
from verification_diagnostics import classify_error
app=base.app
KEY='movie:tt0000001'
class RecoveryQueueTests(unittest.TestCase):
 setUp=base.SourcePacingTests.setUp
 tearDown=base.SourcePacingTests.tearDown
 def config(self,indexers=None):
  cfg={'mediafusion_enabled':True,'mediafusion_url':'https://fallback.example.invalid','jackett_enabled':True,'jackett_url':'http://jackett:9117','jackett_indexers':{'movie':indexers or ['yts']}}
  (app.DATA/'source-providers.json').write_text(json.dumps(cfg));(app.DATA/'jackett-api-key').write_text('c'*32)
  return cfg
 def empty_providers(self,mf=900,jac=1200):
  app.remember('source-provider-control',{'cooldown':time.time()+600,'status':429},90000)
  app.remember('mediafusion:'+KEY,[],mf)
  app.remember(app.jackett_cache_key(KEY),[],jac)
 def test_recovery_is_named_uses_earliest_deadline_and_skips_network(self):
  self.config();self.empty_providers()
  with app.db() as c:before=c.execute('SELECT state,proof FROM validation').fetchall()
  with patch.object(app,'fetch_raw',side_effect=AssertionError('No network during cooldown')):
   with self.assertRaises(app.SourceProviderWait) as raised:app.stream_candidates(KEY)
   error=raised.exception
   self.assertEqual(error.reason,'recovery');self.assertGreater(error.retry_after,598);self.assertLessEqual(error.retry_after,600)
   self.assertEqual(classify_error(error)['code'],'provider_recovery')
   for _ in range(3):
    with self.assertRaises(app.SourceProviderWait):app.stream_candidates(KEY)
  with app.db() as c:self.assertEqual(c.execute('SELECT state,proof FROM validation').fetchall(),before)
  self.assertIsNone(app.cached('source-status:'+KEY))
 def test_fallback_negative_expiry_can_retry_before_primary_recovers(self):
  self.config();self.empty_providers(mf=120,jac=300)
  e=app.source_recovery_wait(KEY)
  self.assertGreater(e.retry_after,118);self.assertLessEqual(e.retry_after,120)
 def test_recovery_deadline_resumes_primary_lookup(self):
  self.config();now=time.time()
  with patch.object(app.time,'time',return_value=now):self.empty_providers();app.source_recovery_wait(KEY)
  with patch.object(app.time,'time',return_value=now+601),patch.object(app,'fetch',return_value={'streams':[{'infoHash':'a'*40,'title':'Example 2020 1080p x264'}]}) as network:
   rows=app.stream_candidates(KEY)
  self.assertEqual(rows[0]['_provider'],'torrentio');self.assertEqual(network.call_count,1)
 def test_new_positive_fallback_bypasses_old_recovery_marker(self):
  self.config();self.empty_providers();app.source_recovery_wait(KEY)
  app.remember('mediafusion:'+KEY,[{'infoHash':'a'*40,'title':'Example 2020 1080p x264','_provider':'mediafusion'}],300)
  with patch.object(app,'fetch_raw',side_effect=AssertionError('No upstream request')):
   self.assertEqual(app.stream_candidates(KEY)[0]['_provider'],'mediafusion')
 def test_new_indexer_configuration_invalidates_negative_namespace_and_wait(self):
  self.config();self.empty_providers();app.source_recovery_wait(KEY);old=app.jackett_cache_key(KEY)
  self.config(['yts','knaben'])
  self.assertNotEqual(app.jackett_cache_key(KEY),old);self.assertIsNone(app.retained_source_recovery(KEY))
 def test_genuine_all_provider_empty_is_not_recovery(self):
  self.config();app.remember('mediafusion:'+KEY,[],900);app.remember(app.jackett_cache_key(KEY),[],900)
  with patch.object(app,'fetch',return_value={'streams':[]}):self.assertEqual(app.stream_candidates(KEY),[])
  self.assertIsNone(app.cached('source-recovery:'+KEY));self.assertEqual(app.cached('source-status:'+KEY)['state'],'unavailable')
 def test_second_indexer_is_used_only_when_first_has_no_candidate(self):
  self.config(['yts','knaben'])
  for i in ['yts','knaben']:app.remember('jackett-caps:'+i,CAPS.decode(),86400)
  calls=[]
  def lookup(url,**kw):
   calls.append(url)
   return b'<rss><channel/></rss>' if '/yts/' in url else listing()
  with patch.object(app,'fetch',side_effect=lookup):rows=app.jackett_streams(KEY)
  self.assertEqual(len(calls),2);self.assertEqual(len(rows),1)
  other='movie:tt0000002'
  app.remember('jackett-yts-control',{},90000)
  with patch.object(app,'fetch',return_value=listing(imdb='tt0000002')) as lookup:
   self.assertEqual(len(app.jackett_streams(other)),1)
  self.assertEqual(lookup.call_count,1)
 def test_recovery_wait_keeps_first_sweep_queued_until_its_retry(self):
  key=base.KEY;app.REL.db().close();app.F.start_verification_sweep()
  with app.F.db() as c:c.execute("UPDATE verification_sweep_attempts SET outcome='queued',started=0,finished=0 WHERE key=?",(key,))
  error=app.SourceProviderWait('Source providers awaiting recovery','mixed','recovery',600)
  with ExitStack() as stack:
   for name in ('seed_validation','repair_cached_series'):stack.enter_context(patch.object(app.F,name))
   stack.enter_context(patch.object(app.REL,'schedule'));stack.enter_context(patch.object(app.F,'foreground_busy',return_value=False));stack.enter_context(patch.object(app.F,'jf',return_value=[]))
   stack.enter_context(patch.object(app.F,'first_sweep_candidate',return_value=((1,key),True)))
   stack.enter_context(patch.object(app.F,'metadata_admission_blocked',return_value=False));stack.enter_context(patch.object(app,'prepared',side_effect=error))
   app.F.warm_availability(limit=1)
  with app.F.db() as c:
   self.assertEqual(tuple(c.execute('SELECT outcome,finished FROM verification_sweep_attempts WHERE key=?',(key,)).fetchone()),('queued',0))
   self.assertGreater(c.execute('SELECT expires FROM metadata_waits WHERE key=?',(key,)).fetchone()[0],time.time()+598)
   events=[json.loads(r[0]) for r in c.execute('SELECT payload FROM verification_attempt_outbox')]
  self.assertTrue(any(e.get('code')=='provider_recovery' and e.get('scope')=='preparation' for e in events))
if __name__=='__main__':unittest.main()
