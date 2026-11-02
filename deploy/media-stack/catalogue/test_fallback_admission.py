import unittest,threading,time,json
from unittest.mock import patch
import test_preparation_pool as base
app=base.app
KEY=base.KEY
class AdmissionTests(unittest.TestCase):
 setUp=base.SourcePacingTests.setUp
 tearDown=base.SourcePacingTests.tearDown
 def test_capacity_backoff_tracks_queue_depth_and_jitter(self):
  self.assertEqual(app.provider_capacity_retry_after(8,15,0),120)
  self.assertEqual(app.provider_capacity_retry_after(8,15,15),135)
  with self.assertRaises(ValueError):app.provider_capacity_retry_after(0,15,0)
  with self.assertRaises(ValueError):app.provider_capacity_retry_after(8,15,16)

 def test_full_fallback_queue_defers_without_consuming_another_preparation_slot(self):
  (app.DATA/'source-providers.json').write_text(json.dumps({'mediafusion_enabled':True,'mediafusion_url':'https://fallback.example.invalid','mediafusion_queue_slots':1}))
  entered=threading.Event();release=threading.Event();errors=[]
  def fetch(*args,**kwargs):entered.set();release.wait(3);return {'streams':[]}
  def owner():
   app.BUDGET.validation_background=True
   try:app.mediafusion_streams('movie:tt0000001')
   except Exception as e:errors.append(type(e).__name__)
  key='movie:tt0000002';app.remember('streams:'+key,[],300)
  with patch.object(app,'fetch',side_effect=fetch) as network:
   t=threading.Thread(target=owner);t.start();self.assertTrue(entered.wait(1))
   app.BUDGET.validation_background=True;app.BUDGET.deadline=time.monotonic()+.4
   try:
    started=time.monotonic()
    with self.assertRaises(app.SourceProviderWait) as error:app.stream_candidates(key)
    self.assertEqual(error.exception.reason,'capacity')
    self.assertGreaterEqual(error.exception.retry_after,30)
    self.assertLessEqual(error.exception.retry_after,45)
    self.assertLess(time.monotonic()-started,.2)
    with self.assertRaises(app.SourceProviderWait) as priority_error:app.mediafusion_streams(key,priority=1)
    self.assertEqual(priority_error.exception.reason,'capacity')
    app.BUDGET.validation_background=False
    with self.assertRaises(app.SourceProviderWait) as foreground_error:app.mediafusion_streams(key)
    self.assertEqual(foreground_error.exception.reason,'busy')
    app.BUDGET.validation_background=True
    self.assertEqual(network.call_count,1)
    self.assertEqual(app.cached('streams:'+key),[])
    self.assertIsNone(app.cached('source-status:'+key))
    self.assertFalse(release.is_set())
   finally:release.set();t.join(2)
  self.assertEqual(errors,[])
  self.assertEqual(list((app.DATA/'mediafusion-provider.lock.waiters').glob('*.ticket')),[])
 def test_capacity_deferral_keeps_first_sweep_unfinished_and_releases_claim(self):
     app.REL.db().close()
     other='movie:tt0000002'
     with app.F.db() as c:
         c.execute("INSERT INTO validation(key,state,priority) VALUES (?,'pending',1)",(other,))
         c.execute('UPDATE validation SET priority=100 WHERE key=?',(KEY,))
     app.F.start_verification_sweep()
     with app.F.db() as c:
         c.execute("UPDATE verification_sweep_attempts SET outcome='queued',started=0,finished=0 WHERE sweep_id=1 AND key=?",(KEY,))
     from contextlib import ExitStack
     with ExitStack() as stack:
         for name in ('seed_validation','repair_cached_series'):
             stack.enter_context(patch.object(app.F,name))
         stack.enter_context(patch.object(app.REL,'schedule'))
         stack.enter_context(patch.object(app.F,'foreground_busy',return_value=False))
         stack.enter_context(patch.object(app.F,'jf',return_value=[]))
         stack.enter_context(patch.object(app.F,'first_sweep_candidate',return_value=((1,KEY),True)))
         stack.enter_context(patch.object(app.F,'metadata_admission_blocked',return_value=False))
         stack.enter_context(patch.object(app,'prepared',side_effect=app.SourceProviderWait('Source provider request deferred',reason='capacity',retry_after=120)))
         app.F.warm_availability(limit=1)
     with app.F.db() as c:
         self.assertEqual(tuple(c.execute('SELECT outcome,finished FROM verification_sweep_attempts WHERE key=?',(KEY,)).fetchone()),('queued',0))
         self.assertEqual(c.execute('SELECT count(*) FROM verification_claims').fetchone()[0],0)
         wait=c.execute('SELECT expires FROM metadata_waits WHERE key=?',(KEY,)).fetchone()
     self.assertIsNotNone(wait)
     self.assertGreaterEqual(wait[0],time.time()+119)
     self.assertEqual(app.F.first_sweep_candidate()[0][1],other)

 def test_priority_overflow_cannot_displace_an_admitted_normal_waiter(self):
  lock=app.DATA/'admission.lock';queue=app.DATA/'admission.lock.waiters'
  held=threading.Event();release=threading.Event();normal_entered=threading.Event();errors=[]
  def owner():
   app.BUDGET.validation_background=False
   with app.serialized_provider_slot(lock,'admission-control'):
    held.set();release.wait(3)
  def normal():
   app.BUDGET.validation_background=True
   try:
    with app.serialized_provider_slot(lock,'admission-control',queue_slots=1):normal_entered.set()
   except Exception as e:errors.append(type(e).__name__)
  first=threading.Thread(target=owner);first.start();self.assertTrue(held.wait(1))
  waiter=threading.Thread(target=normal);waiter.start()
  try:
   until=time.monotonic()+1
   while not list(queue.glob('*.ticket')) and time.monotonic()<until:time.sleep(.01)
   self.assertEqual(len(list(queue.glob('*.ticket'))),1)
   app.BUDGET.validation_background=True;app.BUDGET.deadline=time.monotonic()+.3
   with self.assertRaises(app.SourceProviderWait) as e:
    with app.serialized_provider_slot(lock,'admission-control',priority=1,queue_slots=1):self.fail('Overflow entered')
   self.assertEqual(e.exception.reason,'capacity')
   self.assertEqual(len(list(queue.glob('*.ticket'))),1)
   self.assertFalse(normal_entered.is_set())
  finally:release.set();first.join(2);waiter.join(2)
  self.assertTrue(normal_entered.is_set());self.assertEqual(errors,[])
  self.assertEqual(list(queue.glob('*.ticket')),[])

if __name__=='__main__':unittest.main()
