"""Two admitted callers fit the bounded fallback queue without rate increases."""
import json,time,threading,unittest
from unittest.mock import patch
import test_preparation_pool as fixtures
app=fixtures.app
class QueueAlignmentTests(unittest.TestCase):
 setUp=fixtures.SourcePacingTests.setUp
 tearDown=fixtures.SourcePacingTests.tearDown
 def test_eight_callers_two_admitted_keep_fifo_and_fifteen_second_spacing(self):
  (app.DATA/'source-providers.json').write_text(json.dumps({'mediafusion_enabled':True,'mediafusion_url':'https://fallback.example.invalid','mediafusion_queue_slots':2,'jackett_enabled':True}))
  entered=threading.Event();release=threading.Event();requests=[];errors=[];clock=[time.time()]
  lock=app.DATA/'mediafusion-provider.lock.waiters'
  def fetch(*args,**kwargs):
   requests.append(clock[0])
   if len(requests)==1:entered.set();self.assertTrue(release.wait(4))
   return {'streams':[]}
  def caller(number):
   app.BUDGET.validation_background=True
   try:app.mediafusion_streams('movie:tt'+str(number).zfill(7))
   except Exception as error:errors.append(error)
  with patch.object(app,'fetch',side_effect=fetch),patch.object(app.time,'time',side_effect=lambda:clock[0]):
   first=threading.Thread(target=caller,args=(1,));first.start()
   second=None
   try:
    self.assertTrue(entered.wait(1));second=threading.Thread(target=caller,args=(2,));second.start()
    until=time.monotonic()+1
    while len(list(lock.glob('*.ticket')))<2 and time.monotonic()<until:threading.Event().wait(.01)
    self.assertEqual(len(list(lock.glob('*.ticket'))),2)
    app.BUDGET.validation_background=True
    for number in range(3,9):
     started=time.monotonic()
     with self.assertRaises(app.SourceProviderWait) as caught:app.mediafusion_streams('movie:tt'+str(number).zfill(7))
     self.assertEqual(caught.exception.reason,'capacity');self.assertLess(time.monotonic()-started,.5)
     self.assertEqual(len(list(lock.glob('*.ticket'))),2)
    self.assertEqual(len(requests),1)
    release.set();first.join(2);self.assertFalse(first.is_alive())
    self.assertGreaterEqual(app.cached('mediafusion-provider-control')['next'],clock[0]+15)
    clock[0]+=16;second.join(2);self.assertFalse(second.is_alive())
   finally:
    release.set();clock[0]+=60;first.join(2)
    if second:second.join(2)
  self.assertEqual(errors,[]);self.assertEqual(len(requests),2)
  self.assertGreaterEqual(requests[1]-requests[0],15)
  self.assertEqual(list(lock.glob('*.ticket')),[])
if __name__=='__main__':unittest.main()
