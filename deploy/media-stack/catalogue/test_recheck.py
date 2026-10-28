"""Report/delete is a durable fresh verification request, never file deletion."""
import json,time,uuid,unittest
from pathlib import Path
from unittest.mock import patch
import test_features as fixtures
a=fixtures.a
KEY='movie:tt1234567'
class RecheckTests(unittest.TestCase):
 setUp=fixtures.FeatureTests.setUp
 verify_fixture=fixtures.FeatureTests.verify_fixture
 def report(self,keys,ident=None,requested=None):
  ident=ident or uuid.uuid4().hex;now=requested or time.time()
  path=a.LIBRARY/'.recheck-requests/pending'/(ident+'.json');path.parent.mkdir(parents=True,exist_ok=True)
  path.write_text(json.dumps({'id':ident,'requested':now,'keys':keys}));return path,now
 def test_report_hides_title_preserves_files_and_proof_and_queues_without_backoff(self):
  a.F.validation_result(KEY,True,proof={'fixture':'original','audio_policy':2})
  a.remember('prepared:'+KEY,{'old':'success'},100);a.remember('resolved:'+KEY,{'current':'viewer'},100)
  a.remember('verified-prepared:'+KEY,{'old':'proof'},100);a.remember('verified-prepared:'+KEY+':1',{'old':'audio proof'},100)
  movie=a.F.media/'movie.mp4';movie.write_bytes(b'keep actual saved video')
  metadata=a.LIBRARY/'movie.strm';metadata.parent.mkdir(parents=True,exist_ok=True);metadata.write_text('keep virtual item')
  path,now=self.report([KEY]);a.F.consume_rechecks()
  self.assertFalse(path.exists());self.assertTrue((a.LIBRARY/'.recheck-requests/processed'/path.name).exists())
  self.assertEqual(a.F.source_state(KEY),'pending');self.assertNotIn(KEY,a.F.ready_keys())
  self.assertIsNone(a.cached('prepared:'+KEY));self.assertEqual(a.cached('resolved:'+KEY),{'current':'viewer'})
  self.assertIsNone(a.cached('verified-prepared:'+KEY));self.assertIsNone(a.cached('verified-prepared:'+KEY+':1'))
  with a.F.db() as c:
   row=c.execute('SELECT state,priority,next_check,proof FROM validation WHERE key=?',(KEY,)).fetchone()
  self.assertEqual(row[:3],('pending',1000,0));self.assertEqual(json.loads(row[3])['fixture'],'original')
  self.assertEqual(movie.read_bytes(),b'keep actual saved video');self.assertEqual(metadata.read_text(),'keep virtual item')
 def test_pre_report_or_foreground_success_cannot_undo_pending_report(self):
  a.F.validation_result(KEY,True);path,now=self.report([KEY]);a.F.consume_rechecks()
  a.F.validation_result(KEY,True,attempt_started=now-1,background=True)
  self.assertEqual(a.F.source_state(KEY),'pending')
  a.F.validation_result(KEY,True,attempt_started=now+1,background=False)
  self.assertEqual(a.F.source_state(KEY),'pending')
  a.F.validation_result(KEY,True,proof={'fresh':True},attempt_started=now+1,background=True)
  self.assertEqual(a.F.source_state(KEY),'verified');self.assertIn(KEY,a.F.ready_keys())
  # A late completion from the old generation cannot hide the new pass either.
  a.F.validation_result(KEY,False,attempt_started=now-1,background=True)
  self.assertEqual(a.F.source_state(KEY),'verified')
 def test_failed_recheck_stays_hidden_but_retry_can_restore_title(self):
  a.F.validation_result(KEY,True);path,now=self.report([KEY]);a.F.consume_rechecks()
  a.F.validation_result(KEY,False,reason='No working data',attempt_started=now+1,background=True)
  self.assertEqual(a.F.manual_recheck_state(KEY),'failed');self.assertNotIn(KEY,a.F.ready_keys())
  a.F.validation_result(KEY,True,attempt_started=now+2,background=True)
  self.assertEqual(a.F.manual_recheck_state(KEY),'passed');self.assertIn(KEY,a.F.ready_keys())
 def test_event_replay_and_process_restart_do_not_reset_passed_verification(self):
  path,now=self.report([KEY]);a.F.consume_rechecks();a.F.validation_result(KEY,True,attempt_started=now+1,background=True)
  self.report([KEY],ident=path.stem,requested=now)
  fresh=a.Features(a.__dict__);fresh.consume_rechecks()
  self.assertEqual(fresh.source_state(KEY),'verified')
  with fresh.db() as c:self.assertEqual(c.execute('SELECT COUNT(*) FROM recheck_requests').fetchone()[0],1)
 def test_whole_series_and_season_are_prefix_scoped(self):
  keys=['series:tt1234567:1:1','series:tt1234567:1:2','series:tt1234567:10:1','series:tt12345678:1:1']
  for key in keys:a.F.validation_result(key,True)
  self.report(['series:tt1234567:1']);a.F.consume_rechecks()
  self.assertEqual(a.F.source_state(keys[0]),'pending');self.assertEqual(a.F.source_state(keys[1]),'pending')
  self.assertEqual(a.F.source_state(keys[2]),'verified');self.assertEqual(a.F.source_state(keys[3]),'verified')
  self.report(['series:tt1234567']);a.F.consume_rechecks()
  self.assertEqual(a.F.source_state(keys[2]),'pending');self.assertEqual(a.F.source_state(keys[3]),'verified')
 def test_manual_episode_gets_next_worker_slot_ahead_of_discovery_and_retry(self):
  self.make_search_index=fixtures.FeatureTests.make_search_index.__get__(self);self.make_search_index()
  episode='series:tt2345678:7:18';a.F.enqueue(KEY,20);a.F.validation_result(episode,True)
  self.report([episode]);a.F.consume_rechecks()
  with patch.object(a.F,'jf',return_value=[]),patch.object(a,'prepared',return_value={}) as prepared,patch.object(a,'metadata',return_value={}):a.F.warm_availability(1)
  self.assertEqual(prepared.call_args.args[0],episode)
 def test_downloaded_copy_is_hidden_during_report_without_removing_file(self):
  a.F.validation_result(KEY,True);self.report([KEY]);a.F.consume_rechecks()
  with patch.object(a.F,'local_download',return_value={'local':'existing.mp4'}):
   self.assertEqual(a.F.source_state(KEY),'pending');self.assertNotIn(KEY,a.F.ready_keys())
 def test_invalid_scope_rejected_and_unknown_series_gets_a_first_episode(self):
  path,now=self.report(['../../outside']);a.F.consume_rechecks()
  self.assertTrue((a.LIBRARY/'.recheck-requests/processed'/(path.name+'.rejected')).exists())
  self.report(['series:tt7654321']);a.F.consume_rechecks()
  self.assertEqual(a.F.source_state('series:tt7654321:1:1'),'pending')

if __name__=='__main__':unittest.main()
