"""Per-title viewing holds preserve readiness/files/history and do not stall others."""
import time,unittest,json
from unittest.mock import patch
import test_features as fixtures
a=fixtures.a
KEY='movie:tt1234567';OTHER='movie:tt2345678'
class HoldTests(unittest.TestCase):
 setUp=fixtures.FeatureTests.setUp
 make_ready=fixtures.FeatureTests.make_ready
 fake_jf=fixtures.FeatureTests.fake_jf
 def hold(self,key=KEY,expires=None):
  with a.F.db() as c:c.execute('INSERT OR REPLACE INTO verification_holds VALUES (?,?,?)',(key,expires if expires is not None else time.time()+86400,'Owner resuming later'))
 def run_worker(self,expected):
  with patch.object(a.F,'jf',return_value=[]),patch.object(a.F,'seed_validation'),patch.object(a,'prepared',return_value={}) as prepare,patch('features.time.sleep'):
   a.F.warm_availability(1)
  prepare.assert_called_once_with(expected,background=True,cancel=unittest.mock.ANY)
 def test_initial_audit_skips_hold_preserves_visibility_and_original_proof(self):
  for key in (KEY,OTHER):a.F.validation_result(key,True,proof={'version':4,'fixture':key})
  self.hold();self.run_worker(OTHER)
  self.assertEqual(a.F.source_state(KEY),'verified');self.assertIn(KEY,a.F.ready_keys())
  with a.F.db() as c:
   state,proof=c.execute('SELECT state,proof FROM validation WHERE key=?',(KEY,)).fetchone()
   self.assertEqual(json.loads(proof)['fixture'],KEY)
   self.assertEqual(c.execute('SELECT completed FROM verification_audit WHERE key=?',(KEY,)).fetchone()[0],0)
 def test_manual_report_and_checkpoint_hold_do_not_block_other_work(self):
  a.F.enqueue(KEY,1000);a.F.enqueue(OTHER,10);a.REL.db().close();self.hold()
  with a.F.db() as c:
   c.execute('INSERT INTO manual_rechecks VALUES (?,?,?,?)',(KEY,time.time(),'fixture','pending'))
  a.REL.checkpoint(KEY,state='checkpoint')
  self.run_worker(OTHER)
 def test_due_failure_hold_does_not_block_retry_queue(self):
  a.F.validation_result(KEY,False);self.hold();a.F.enqueue(OTHER,20)
  with a.F.db() as c:c.execute('UPDATE validation SET next_check=0 WHERE key=?',(KEY,))
  self.run_worker(OTHER)
 def test_ordinary_pending_and_episode_holds_are_excluded(self):
  a.F.enqueue(KEY,1000);a.F.enqueue(OTHER,20);self.hold();self.run_worker(OTHER)
  episode='series:tt7654321:2:3';a.F.enqueue(episode,1000);self.hold(episode);self.run_worker(OTHER)
 def test_recurring_audit_expiry_and_reopened_database(self):
  for key in (KEY,OTHER):a.F.validation_result(key,True,proof={'version':5})
  a.REL.db().close()
  with a.F.db() as c:c.execute('UPDATE validation SET checked=1')
  self.hold()
  self.assertEqual(a.F.worker_audit(recurring=True)[0],OTHER)
  self.hold(expires=time.time()-2)
  self.assertFalse(a.F.verification_held(KEY));self.assertEqual(a.F.worker_audit(recurring=True)[0],KEY)
 def test_explicit_background_attempt_cannot_touch_held_source(self):
  a.F.validation_result(KEY,True,proof={'version':4});self.hold()
  with patch.object(a,'resolve',side_effect=AssertionError('Held movie must not resolve')),self.assertRaises(a.Checkpoint):a.prepared(KEY,background=True)
  self.assertEqual(a.F.source_state(KEY),'verified')
 def test_managed_cleanup_preserves_held_download_even_if_played(self):
  saved,manual=self.make_ready();self.hold()
  with patch.object(a.F,'jf',side_effect=self.fake_jf()):a.F.cleanup_saved()
  self.assertTrue(saved.exists());self.assertTrue(manual.exists());self.assertEqual(a.F.records()[KEY]['state'],'ready')
  self.hold(expires=time.time()-2)
  with patch.object(a.F,'jf',side_effect=self.fake_jf()):a.F.cleanup_saved()
  self.assertFalse(saved.exists());self.assertTrue(manual.exists())
if __name__=='__main__':unittest.main()
