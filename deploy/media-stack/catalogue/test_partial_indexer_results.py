"""A successful indexer lookup survives a later provider's admission wait."""
import time,unittest,threading,collections
from urllib.parse import urlsplit,parse_qs
from contextlib import contextmanager
from unittest.mock import patch
import test_source_recovery as support
from test_source_discovery import CAPS,listing
app=support.app;KEY=support.KEY
EMPTY=b'<rss><channel/></rss>'
class PartialIndexerTests(unittest.TestCase):
 setUp=support.RecoveryQueueTests.setUp
 tearDown=support.RecoveryQueueTests.tearDown
 config=support.RecoveryQueueTests.config
 def prepare(self):
  self.config(['yts','knaben'])
  for name in ['yts','knaben']:app.remember('jackett-caps:'+name,CAPS.decode(),86400)
 def test_retry_does_not_repeat_completed_first_search_when_second_was_full(self):
  self.prepare();blocked=[True];calls=[]
  @contextmanager
  def slot(path,control,*args,**kwargs):
   if control=='jackett-knaben-control' and blocked[0]:raise app.SourceProviderWait('Full',control,'capacity',30)
   yield {'queue_wait_ms':0}
  def first(url,**kwargs):calls.append('first');return EMPTY
  with patch.object(app,'serialized_provider_slot',side_effect=slot),patch.object(app,'fetch',side_effect=first):
   with self.assertRaises(app.SourceProviderWait):app.jackett_streams(KEY)
  blocked[0]=False
  def retry(url,**kwargs):
   self.assertIn('/knaben/',url,'A completed empty primary search must be retained');calls.append('second');return listing()
  with patch.object(app,'serialized_provider_slot',side_effect=slot),patch.object(app,'fetch',side_effect=retry):rows=app.jackett_streams(KEY)
  self.assertEqual(calls,['first','second']);self.assertEqual(len(rows),1)
 def test_failed_first_search_does_not_erase_successful_empty_second_search(self):
  self.prepare();blocked=[True]
  @contextmanager
  def slot(path,control,*args,**kwargs):
   if control=='jackett-yts-control' and blocked[0]:raise app.SourceProviderWait('Full',control,'capacity',30)
   yield {'queue_wait_ms':0}
  with patch.object(app,'serialized_provider_slot',side_effect=slot),patch.object(app,'fetch',return_value=EMPTY):
   with self.assertRaises(app.SourceProviderWait):app.jackett_streams(KEY)
  blocked[0]=False
  def retry(url,**kwargs):self.assertIn('/yts/',url);return EMPTY
  with patch.object(app,'serialized_provider_slot',side_effect=slot),patch.object(app,'fetch',side_effect=retry) as network:self.assertEqual(app.jackett_streams(KEY),[])
  self.assertEqual(network.call_count,1)
 def test_recovery_respects_partial_negative_expiry_instead_of_repolling(self):
  self.prepare();now=time.time()
  app.remember('source-provider-control',{'cooldown':now+600},90000)
  app.remember('mediafusion:'+KEY,[],900)
  app.remember('jackett-yts-control',{'cooldown':now+900},90000)
  app.remember(app.jackett_indexer_cache_key('knaben',KEY),[],120)
  error=app.source_recovery_wait(KEY)
  self.assertGreater(error.retry_after,118);self.assertLessEqual(error.retry_after,120)

 def test_eight_callers_finish_both_searches_without_repeating_completed_requests(self):
  self.prepare();counter=collections.Counter();mutex=threading.Lock();finished=[];errors=[]
  real_success=app.provider_succeeded
  def paced(control,spacing):
   real_success(control,spacing)
   state=app.cached(control);state['next']=time.time()+.01
   app.remember(control,state,90000)
  def network(url,**kwargs):
   query=parse_qs(urlsplit(url).query);indexer=url.split('/indexers/')[1].split('/')[0]
   with mutex:counter[(indexer,query['imdbid'][0])]+=1
   time.sleep(.01);return EMPTY
  def caller(number):
   key='movie:tt'+str(number).zfill(7);end=time.monotonic()+6
   app.BUDGET.validation_background=True
   try:
    while time.monotonic()<end:
     app.BUDGET.deadline=min(end,time.monotonic()+.3)
     try:
      value=app.jackett_streams(key)
      if value!=[]:raise AssertionError('Expected a completed empty search')
      with mutex:finished.append(key)
      return
     except app.SourceProviderWait:time.sleep(.01)
    raise AssertionError('Caller did not finish within the bounded test')
   except Exception as error:
    with mutex:errors.append(type(error).__name__)
  with patch.object(app,'fetch',side_effect=network),patch.object(app,'provider_succeeded',side_effect=paced):
   threads=[threading.Thread(target=caller,args=(i,)) for i in range(1,9)]
   for thread in threads:thread.start()
   for thread in threads:thread.join(8)
  self.assertEqual(errors,[]);self.assertEqual(len(finished),8)
  self.assertEqual(sum(counter.values()),16)
  self.assertTrue(all(count==1 for count in counter.values()))

 def test_combined_negative_never_outlives_earliest_completed_lookup(self):
  self.prepare();clock=[time.time()]
  @contextmanager
  def slot(path,control,*args,**kwargs):
   if control=='jackett-knaben-control' and clock[0]==initial:raise app.SourceProviderWait('Full',control,'capacity',30)
   yield {'queue_wait_ms':0}
  initial=clock[0]
  with patch.object(app.time,'time',side_effect=lambda:clock[0]),patch.object(app,'serialized_provider_slot',side_effect=slot),patch.object(app,'fetch',return_value=EMPTY):
   with self.assertRaises(app.SourceProviderWait):app.jackett_streams(KEY)
   clock[0]+=600
   self.assertEqual(app.jackett_streams(KEY),[])
   expiry=app.cached_expiry(app.jackett_cache_key(KEY))
   self.assertLessEqual(expiry,initial+901)
   self.assertGreater(expiry,initial+899)
if __name__=='__main__':unittest.main()
