"""Partial provider coverage advances sweep atomically without inventing proof."""
import json,time,sqlite3,unittest
from contextlib import ExitStack,closing
from unittest.mock import patch
import test_verification_title as fixture
from source_coverage import SourceCoverage,PartialSourceCoverage,source_retry_delay
from verification_diagnostics import classify_error,DiagnosticLedger
from test_source_discovery import CAPS,listing
a=fixture.a;KEY=fixture.KEY

class PartialCoverageTests(unittest.TestCase):
 def setUp(self):
  self.t=fixture.TitleTests();self.t.setUp();self.addCleanup(self.t.doCleanups)
  attrs=('validation_background','first_sweep_attempt','source_coverage','source_coverage_key','deadline','worker_deadline','cancel','remote_verification','remote_cancel_code','speculative')
  old={n:(hasattr(a.BUDGET,n),getattr(a.BUDGET,n,None)) for n in attrs}
  def restore():
   for n,(existed,v) in old.items():
    if existed:setattr(a.BUDGET,n,v)
    elif hasattr(a.BUDGET,n):delattr(a.BUDGET,n)
  self.addCleanup(restore)
  a.BUDGET.validation_background=True;a.BUDGET.first_sweep_attempt=True;a.BUDGET.deadline=None;a.BUDGET.worker_deadline=None;a.BUDGET.cancel=None;a.BUDGET.remote_cancel_code='';a.BUDGET.speculative=False
  cfg={'mediafusion_enabled':True,'mediafusion_url':'https://fallback.example.invalid','jackett_enabled':True,'jackett_url':'http://jackett:9117','jackett_indexers':{'movie':['yts']}}
  (a.DATA/'source-providers.json').write_text(json.dumps(cfg));(a.DATA/'jackett-api-key').write_text('c'*32)
  with a.F.db() as c:c.execute("INSERT INTO validation(key,state,attempts,checked,proof) VALUES (?,'pending',3,123,?)",(KEY,json.dumps({'retained':'proof'})))
  a.F.start_verification_sweep()
  with a.F.db() as c:c.execute("UPDATE verification_sweep_attempts SET outcome='queued',started=0,finished=0 WHERE key=?",(KEY,))
 def evidence(self):
  now=time.time();c=SourceCoverage(['torrentio','mediafusion','jackett:yts'])
  c.record('torrentio','unavailable',now,now+10000,reason='cooldown')
  c.record('mediafusion','empty',now,now+900)
  c.record('jackett:yts','empty',now,now+900)
  e=c.partial(now);e['configuration']=a.source_config_signature();return e
 def negatives(self):
  a.remember('source-provider-control',{'status':429,'cooldown':time.time()+10000},90000)
  a.remember('mediafusion:'+KEY,[],900)
  a.remember(a.jackett_indexer_cache_key('yts',KEY),[],900)
 def state(self):
  with a.F.db() as c:return c.execute('SELECT state,attempts,checked,proof FROM validation WHERE key=?',(KEY,)).fetchone()
 def warm(self,error):
  with ExitStack() as s:
   for name in ('seed_validation','repair_cached_series'):s.enter_context(patch.object(a.F,name))
   s.enter_context(patch.object(a.REL,'schedule'));s.enter_context(patch.object(a.F,'foreground_busy',return_value=False));s.enter_context(patch.object(a.F,'jf',return_value=[]))
   s.enter_context(patch.object(a.F,'first_sweep_candidate',return_value=((1,KEY),True)))
   s.enter_context(patch.object(a.F,'metadata_admission_blocked',return_value=False));s.enter_context(patch.object(a,'prepared',side_effect=error))
   s.enter_context(patch('verification_remote.jobs',return_value=None))
   a.F.warm_availability(1)
 def test_matrix_requires_completed_search_and_all_available_providers(self):
  for states,want in [(('empty','unavailable'),True),(('filtered','unavailable'),True),(('unavailable','unavailable'),False),(('empty','waiting'),False),(('empty','candidates'),False),(('empty','empty'),False),(('empty',None),False)]:
   c=SourceCoverage(['torrentio','mediafusion'])
   for p,state in zip(c.providers,states):
    if state:c.record(p,state,100,200)
   self.assertEqual(bool(c.partial(150)),want,states)
   self.assertIsNone(c.partial(201))
 def test_real_discovery_converts_completed_fallbacks_and_primary_cooldown(self):
  self.negatives();before=self.state()
  with patch.object(a,'fetch_raw',side_effect=AssertionError('No network during cached negative/cooldown')):
   with self.assertRaises((PartialSourceCoverage,a.SourceProviderWait)) as caught:a.stream_candidates(KEY)
  self.assertIsInstance(caught.exception,PartialSourceCoverage)
  e=caught.exception.coverage
  self.assertEqual([r['status'] for r in e['providers']],['unavailable','empty','empty'])
  self.assertEqual(classify_error(caught.exception)['code'],'partial_provider_coverage')
  self.assertEqual(self.state(),before)
 def test_admission_only_wait_does_not_convert(self):
  a.remember('source-provider-control',{'status':429,'cooldown':time.time()+10000},90000)
  with patch.object(a,'fallback_candidates',side_effect=a.SourceProviderWait('capacity',reason='capacity')):
   with self.assertRaises(a.SourceProviderWait):a.stream_candidates(KEY)
 def test_unknown_and_expired_provider_block_completion(self):
  self.negatives();e=a.discovery_coverage(KEY);e.rows['mediafusion']['expires']=time.time()-1
  self.assertIsNone(e.partial(time.time()))
  e=a.discovery_coverage(KEY);e.rows.pop('jackett:yts');self.assertIsNone(e.partial(time.time()))
 def test_foreground_and_manual_checks_keep_existing_recovery(self):
  self.negatives()
  for foreground,manual in ((True,False),(False,True)):
   if manual:
    with a.F.db() as c:c.execute("INSERT INTO manual_rechecks VALUES (?,1,'fixture','pending')",(KEY,))
   with patch.object(a.BUDGET,'validation_background',not foreground),patch.object(a,'fetch_raw',side_effect=AssertionError('No network')):
    with self.assertRaises(a.SourceProviderWait):a.stream_candidates(KEY)
 def test_new_positive_provider_results_win(self):
  self.negatives();a.remember('mediafusion:'+KEY,[{'infoHash':'a'*40,'title':'Fixture 2026 1080p x264','_provider':'mediafusion'}],300)
  with patch.object(a,'fetch_raw',side_effect=AssertionError('No network')):self.assertEqual(len(a.stream_candidates(KEY)),1)
 def test_fresh_search_event_provides_real_empty_evidence(self):
  a.remember('source-provider-control',{'status':429,'cooldown':time.time()+10000},90000)
  a.remember('jackett-caps:yts',CAPS.decode(),86400)
  def fetch(url,**kw):
   if 'fallback.example.invalid' in url:return {'streams':[]}
   if 'jackett:9117' in url:return b'<rss><channel/></rss>'
   raise a.SourceProviderWait('cooldown',reason='cooldown',retry_after=10000)
  with patch.object(a,'fetch',side_effect=fetch):
   with self.assertRaises(PartialSourceCoverage) as caught:a.stream_candidates(KEY)
  self.assertEqual([r['status'] for r in caught.exception.coverage['providers']],['unavailable','empty','empty'])
 def test_worker_commits_one_deferral_and_retry_preserving_all_media_state(self):
  before=self.state();self.warm(PartialSourceCoverage(self.evidence()))
  self.assertEqual(self.state(),before)
  with a.F.db() as c:
   self.assertEqual(c.execute('SELECT outcome FROM verification_sweep_attempts WHERE key=?',(KEY,)).fetchone()[0],'deferred')
   retry=c.execute('SELECT attempts,next_retry FROM source_coverage_retries WHERE key=?',(KEY,)).fetchone();self.assertEqual(retry[0],1);self.assertGreater(retry[1],time.time()+21590)
   self.assertEqual(c.execute('SELECT count(*) FROM verification_claims').fetchone()[0],0)
   events=[json.loads(r[0]) for r in c.execute('SELECT payload FROM verification_attempt_outbox')]
   self.assertEqual(len(events),1);self.assertEqual(events[0]['code'],'partial_provider_coverage');self.assertEqual(events[0]['scope'],'first_sweep')
   self.assertIsNone(c.execute('SELECT key FROM verification_retry_waits').fetchone())
 def test_preparation_preserves_typed_signal_without_checkpoint_mutation(self):
  error=PartialSourceCoverage(self.evidence())
  with patch.object(a.BUDGET,'worker_deadline',time.monotonic()+120),patch.object(a,'prepare_locked',side_effect=error),patch.object(a.REL,'checkpoint') as checkpoint,patch.object(a.F,'enqueue') as enqueue:
   with self.assertRaises(PartialSourceCoverage):a.prepared(KEY,background=True)
  checkpoint.assert_not_called();enqueue.assert_not_called()
 def test_later_partial_retry_advances_only_separate_counter(self):
  self.warm(PartialSourceCoverage(self.evidence()))
  with a.F.db() as c:
   c.execute('UPDATE source_coverage_retries SET next_retry=1');c.execute('DELETE FROM metadata_waits')
  time.sleep(.01)
  self.warm(PartialSourceCoverage(self.evidence()))
  with a.F.db() as c:
   self.assertEqual(c.execute('SELECT attempts FROM source_coverage_retries').fetchone()[0],2)
   self.assertEqual(c.execute('SELECT attempts FROM validation WHERE key=?',(KEY,)).fetchone()[0],3)
   scopes=[json.loads(r[0])['scope'] for r in c.execute('SELECT payload FROM verification_attempt_outbox')]
  self.assertEqual(scopes,['first_sweep','source_coverage_retry'])
 def test_manual_recheck_consumption_clears_partial_retry(self):
  self.warm(PartialSourceCoverage(self.evidence()))
  root=self.t.root/'library';pending=root/'.recheck-requests'/'pending';pending.mkdir(parents=True)
  ident='a'*32
  (pending/(ident+'.json')).write_text(json.dumps({'id':ident,'requested':time.time(),'keys':[KEY]}))
  with patch.object(a,'LIBRARY',root):a.F.consume_rechecks()
  self.assertFalse(a.F.has_coverage_retry(KEY));self.assertEqual(a.F.manual_recheck_state(KEY),'pending')
 def test_long_backoff_matches_existing_failure_policy_through_cap(self):
  self.assertEqual([source_retry_delay(n) for n in range(1,10)],[21600,43200,86400,172800,345600,691200,1382400,2592000,2592000])
  claim=a.F.claim_verification(KEY,'fixture');self.assertTrue(a.F.mark_first_sweep_attempt(KEY));start=time.time()-1
  for n in range(1,10):
   self.assertTrue(a.F.defer_source_coverage(KEY,self.evidence(),claim,n==1,start))
   with a.F.db() as c:
    attempts,due=c.execute('SELECT attempts,next_retry FROM source_coverage_retries WHERE key=?',(KEY,)).fetchone()
   self.assertEqual(attempts,n);self.assertAlmostEqual(due-time.time(),source_retry_delay(n),delta=2)
   start=time.time()+.01
   # Move the attempt start beyond the previous durable receipt without changing source evidence.
   time.sleep(.02)
 def test_duplicate_and_stale_completion_do_not_increment_retry(self):
  claim=a.F.claim_verification(KEY,'fixture');a.F.mark_first_sweep_attempt(KEY);start=time.time()-1
  self.assertTrue(a.F.defer_source_coverage(KEY,self.evidence(),claim,True,start))
  self.assertFalse(a.F.defer_source_coverage(KEY,self.evidence(),claim,True,start))
  with a.F.db() as c:self.assertEqual(c.execute('SELECT attempts FROM source_coverage_retries').fetchone()[0],1)
 def test_outbox_error_rolls_back_completion_and_retry(self):
  claim=a.F.claim_verification(KEY,'fixture');a.F.mark_first_sweep_attempt(KEY)
  with a.F.db() as c:c.execute("CREATE TRIGGER reject_outbox BEFORE INSERT ON verification_attempt_outbox BEGIN SELECT RAISE(ABORT,'fixture rollback'); END")
  with self.assertRaises(sqlite3.IntegrityError):a.F.defer_source_coverage(KEY,self.evidence(),claim,True,time.time()-1)
  with a.F.db() as c:
   self.assertEqual(c.execute('SELECT outcome FROM verification_sweep_attempts WHERE key=?',(KEY,)).fetchone()[0],'started')
   self.assertEqual(c.execute('SELECT count(*) FROM source_coverage_retries').fetchone()[0],0)
   self.assertEqual(c.execute('SELECT count(*) FROM metadata_waits').fetchone()[0],0)
 def test_worker_outbox_failure_cannot_fall_through_to_sweep_completion(self):
  with a.F.db() as c:c.execute("CREATE TRIGGER reject_outbox BEFORE INSERT ON verification_attempt_outbox BEGIN SELECT RAISE(ABORT,'fixture rollback'); END")
  self.warm(PartialSourceCoverage(self.evidence()))
  with a.F.db() as c:
   self.assertEqual(c.execute('SELECT outcome FROM verification_sweep_attempts WHERE key=?',(KEY,)).fetchone()[0],'queued')
   self.assertEqual(c.execute('SELECT count(*) FROM source_coverage_retries').fetchone()[0],0)
   self.assertEqual(c.execute('SELECT count(*) FROM verification_attempt_outbox').fetchone()[0],0)
 def test_manual_request_hold_claim_loss_and_configuration_change_fence_commit(self):
  claim=a.F.claim_verification(KEY,'fixture');a.F.mark_first_sweep_attempt(KEY);e=self.evidence()
  self.assertFalse(a.F.defer_source_coverage(KEY,e,'wrong-token',True,time.time()-1))
  with a.F.db() as c:c.execute("INSERT INTO verification_holds VALUES (?,?,'fixture')",(KEY,time.time()+100))
  self.assertFalse(a.F.defer_source_coverage(KEY,e,claim,True,time.time()-1))
  with a.F.db() as c:
   c.execute('DELETE FROM verification_holds');c.execute("INSERT INTO manual_rechecks VALUES (?,1,'fixture','pending')",(KEY,))
  self.assertFalse(a.F.defer_source_coverage(KEY,e,claim,True,time.time()-1))
  with a.F.db() as c:c.execute('DELETE FROM manual_rechecks')
  e['configuration']='changed';self.assertFalse(a.F.defer_source_coverage(KEY,e,claim,True,time.time()-1))
 def test_due_retry_survives_new_features_instance_and_obeys_guards(self):
  claim=a.F.claim_verification(KEY,'fixture');a.F.mark_first_sweep_attempt(KEY);a.F.defer_source_coverage(KEY,self.evidence(),claim,True,time.time()-1);a.F.release_verification(KEY,claim)
  from features import Features
  fresh=Features(a.__dict__)
  with fresh.db() as c:
   self.assertIsNone(fresh.due_coverage_retry(c,time.time()))
   c.execute('UPDATE source_coverage_retries SET next_retry=1');c.execute('DELETE FROM metadata_waits')
   self.assertEqual(fresh.due_coverage_retry(c,time.time())[0],KEY)
   c.execute("INSERT INTO verification_holds VALUES (?,?,'fixture')",(KEY,time.time()+100))
   self.assertIsNone(fresh.due_coverage_retry(c,time.time()))
 def test_success_clears_retry_and_confirmed_failure_switches_to_normal_route(self):
  for ok in (True,False):
   with a.F.db() as c:
    c.execute('INSERT OR REPLACE INTO source_coverage_retries VALUES (?,1,9999999999,?,1)',(KEY,json.dumps(self.evidence())))
    c.execute('INSERT OR REPLACE INTO metadata_waits VALUES (?,9999999999)',(KEY,))
   self.assertTrue(a.F.validation_result(KEY,ok,reason='fixture',background=True))
   self.assertFalse(a.F.has_coverage_retry(KEY))
 def test_coverage_ledger_evidence_contains_no_exception_or_url_text(self):
  e=self.evidence();e['providers'][0]['secret']='https://credential.invalid/token'
  claim=a.F.claim_verification(KEY,'fixture');a.F.mark_first_sweep_attempt(KEY)
  self.assertTrue(a.F.defer_source_coverage(KEY,e,claim,True,time.time()-1))
  with a.F.db() as c:raw=c.execute('SELECT payload FROM verification_attempt_outbox').fetchone()[0]
  self.assertNotIn('credential.invalid',raw)
  ledger=DiagnosticLedger(self.t.root/'diagnostic.sqlite');ledger.ingest([('fixture',raw)])
  with closing(ledger.connect()) as c:self.assertEqual(c.execute('SELECT code FROM receipts').fetchone()[0],'partial_provider_coverage')
if __name__=='__main__':unittest.main()
