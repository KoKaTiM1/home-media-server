"""Queue fairness must survive admission waits, mixed kinds and restarts."""
import sqlite3,time,unittest
from unittest.mock import patch
from features import Features
import test_first_sweep as baseline

class SweepFairnessTests(unittest.TestCase):
 tearDown=baseline.FirstSweepTests.tearDown
 db=baseline.FirstSweepTests.db
 def setUp(self):
  baseline.FirstSweepTests.setUp(self)
  with sqlite3.connect(self.path) as c:
   # All early index entries are series, just like the observed live queue.
   for i in range(80):
    old=f'movie:tt{i:07}';new=f'series:tt{i:07}:1:1'
    for table in ('validation','verification_sweep_attempts'):c.execute(f'UPDATE {table} SET key=? WHERE key=?',(new,old))
 def test_both_kinds_reached_beyond_first_window_and_restart(self):
  seen=[]
  for _ in range(8):
   candidate,_=self.f.first_sweep_candidate();key=candidate[1];seen.append(key.split(':')[0])
   self.assertTrue(self.f.mark_first_sweep_attempt(key))
   with sqlite3.connect(self.path) as c:c.execute("UPDATE verification_sweep_attempts SET outcome='deferred' WHERE key=?",(key,))
   self.f=Features({});self.f.db=self.db
  self.assertEqual(seen,['movie','series']*4)
 def test_metadata_ready_boost_does_not_starve_other_kind(self):
  with sqlite3.connect(self.path) as c:c.execute("UPDATE validation SET priority=200 WHERE key GLOB 'series:*'")
  kinds=[]
  for _ in range(4):
   key=self.f.first_sweep_candidate()[0][1];kinds.append(key.split(':')[0]);self.f.mark_first_sweep_attempt(key)
   with sqlite3.connect(self.path) as c:c.execute("UPDATE verification_sweep_attempts SET outcome='deferred' WHERE key=?",(key,))
  self.assertEqual(kinds,['movie','series']*2)
 def test_unavailable_kind_does_not_create_idle_turn(self):
  with sqlite3.connect(self.path) as c:
   c.execute("INSERT INTO verification_holds SELECT key,? FROM validation WHERE key GLOB 'series:*'",(time.time()+600,))
   c.execute("INSERT INTO verification_sweep_dispatch VALUES(1,'movie')")
  self.assertTrue(self.f.first_sweep_candidate()[0][1].startswith('movie:'))
 def test_snapshot_priority_overrides_kind_rotation(self):
  with sqlite3.connect(self.path) as c:
   c.execute("UPDATE verification_sweep_attempts SET priority=200 WHERE key='series:tt0000000:1:1'")
   c.execute("UPDATE validation SET priority=200 WHERE key='series:tt0000000:1:1'")
  self.assertEqual(self.f.first_sweep_candidate()[0][1],'series:tt0000000:1:1')
 def test_admission_rotation_survives_expired_wait_without_finishing_or_proof_change(self):
  with sqlite3.connect(self.path) as c:
   before=c.execute("SELECT state,proof FROM validation WHERE key='movie:tt0000080'").fetchone()
  key='movie:tt0000080';self.assertTrue(self.f.mark_first_sweep_attempt(key))
  self.assertTrue(self.f.requeue_first_sweep_infrastructure(key,30))
  with sqlite3.connect(self.path) as c:
   c.execute('DELETE FROM metadata_waits WHERE key=?',(key,))
   after=c.execute('SELECT state,proof FROM validation WHERE key=?',(key,)).fetchone()
   self.assertEqual(before,after)
   self.assertEqual(c.execute('SELECT outcome,finished FROM verification_sweep_attempts WHERE key=?',(key,)).fetchone(),('queued',0))
   # Select movie again: the expired wait must not restore its old place.
   c.execute("INSERT OR REPLACE INTO verification_sweep_dispatch VALUES(1,'series')")
  self.assertEqual(self.f.first_sweep_candidate()[0][1],'movie:tt0000081')
 def test_resolved_metadata_handoff_does_not_bypass_rotation_forever(self):
  key='movie:tt0099999'
  with sqlite3.connect(self.path) as c:c.execute("INSERT INTO verification_metadata_dependencies VALUES(1,?, ?,1,0,0,'resumed')",(key,'meta:'+key))
  self.assertEqual(self.f.first_sweep_candidate()[0][1],key)
  self.assertTrue(self.f.mark_first_sweep_attempt(key));self.f.requeue_first_sweep_infrastructure(key,30)
  with sqlite3.connect(self.path) as c:
   c.execute('DELETE FROM metadata_waits WHERE key=?',(key,))
   self.assertEqual(c.execute('SELECT state FROM verification_metadata_dependencies WHERE key=?',(key,)).fetchone()[0],'closed')
  self.assertNotEqual(self.f.first_sweep_candidate()[0][1],key)

if __name__=='__main__':unittest.main()
