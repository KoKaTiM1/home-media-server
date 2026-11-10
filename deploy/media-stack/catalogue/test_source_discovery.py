"""Discovery outage regressions: raw results never become verification proof."""
import json,time,unittest,urllib.error
from email.utils import formatdate
from unittest.mock import patch
from source_discovery import failure_state,success_state,retry_after_seconds,torznab_streams,search_parameters
import test_preparation_pool as base
app=base.app
CAPS=b'<caps><searching><movie-search available="yes" supportedParams="q,imdbid"/><tv-search available="yes" supportedParams="q,season,ep"/></searching></caps>'
def listing(title='Example.2020.1080p.x264.AAC',imdb='tt0000001',infohash='a'*40):
 return ('<rss xmlns:t="http://torznab.com/schemas/2015/feed"><channel><item><title>'+title+'</title><t:attr name="infohash" value="'+infohash+'"/><t:attr name="imdbid" value="'+imdb+'"/><t:attr name="seeders" value="12"/></item></channel></rss>').encode()
class RecoveryTests(unittest.TestCase):
 def test_retry_after_dates_numbers_and_invalid_values(self):
  self.assertEqual(retry_after_seconds({'Retry-After':formatdate(2200,usegmt=True)},1000),1200)
  for value in ('nan','inf','garbage','-1'):self.assertEqual(retry_after_seconds({'Retry-After':value},1000),0)
  self.assertEqual(retry_after_seconds({'Retry-After':'90000'},1000),90000)
 def test_backoff_grows_and_recovery_remains_paced(self):
  first=failure_state({},429,{},5,1000,7);second=failure_state(first,429,{},5,2000,7)
  self.assertEqual(first['cooldown'],1907);self.assertEqual(second['cooldown'],3807)
  third=failure_state(second,429,{'Retry-After':formatdate(10000,usegmt=True)},5,3000)
  self.assertEqual(third['cooldown'],10000)
  recovered=success_state(third,5,10001)
  self.assertEqual(recovered['cooldown'],0);self.assertGreater(recovered['interval'],5)
  self.assertEqual(recovered['requests'],4)
 def test_identity_capabilities_and_original_title(self):
  self.assertEqual(search_parameters(CAPS,'movie:tt0000001',{})['imdbid'],'tt0000001')
  q=search_parameters(CAPS,'series:tt0000001:2:3',{'name':'Hebrew display','originalName':'Original title'})
  self.assertEqual(q,{'t':'tvsearch','q':'Original title','season':'2','ep':'3'})
  with self.assertRaises(ValueError):search_parameters(b'<caps/>','movie:tt0000001',{})
 def test_malformed_error_or_entity_response_is_not_empty(self):
  for raw in (b'<error code="900"/>',b'<rss><error/></rss>',b'<!DOCTYPE rss><rss/>',b'<html/>',b'bad'):
   with self.assertRaises((ValueError,app.ET.ParseError)):torznab_streams(raw,'movie:tt0000001')
 def test_wrong_identity_rejected_and_urls_never_forwarded(self):
  self.assertEqual(torznab_streams(listing(imdb='tt0000002'),'movie:tt0000001'),[])
  rows=torznab_streams(listing(),'movie:tt0000001')
  self.assertEqual(rows[0]['infoHash'],'a'*40);self.assertNotIn('url',rows[0]);self.assertNotIn('torrentUrl',rows[0])
class ChainTests(unittest.TestCase):
 setUp=base.SourcePacingTests.setUp
 tearDown=base.SourcePacingTests.tearDown
 def configure(self):
  (app.DATA/'source-providers.json').write_text(json.dumps({'mediafusion_enabled':True,'mediafusion_url':'https://fallback.example.invalid','jackett_enabled':True,'jackett_url':'http://jackett:9117','jackett_indexers':{'movie':['yts'],'series':['thepiratebay']}}))
  (app.DATA/'jackett-api-key').write_text('c'*32)
  app.remember('jackett-caps:yts',CAPS.decode(),86400)
 def test_both_providers_fail_independent_indexer_discovers_without_approval(self):
  self.configure();key='movie:tt0000001'
  with app.db() as c:before=c.execute('select state,proof from validation').fetchall()
  def network(url,**kwargs):
   if url.startswith(app.PROVIDER):raise urllib.error.HTTPError(url,429,'limited',{},None)
   if url.startswith('https://fallback.'):raise urllib.error.URLError('private-url-and-secret')
   return listing()
  with patch.object(app,'fetch',side_effect=network),patch.object(app,'log') as logs:
   rows=app.stream_candidates(key)
  self.assertEqual(len(rows),1);self.assertEqual(rows[0]['_provider'],'jackett')
  with app.db() as c:self.assertEqual(c.execute('select state,proof from validation').fetchall(),before)
  self.assertNotIn('private-url-and-secret',str(logs.call_args_list))
  self.assertNotIn('c'*32,str(logs.call_args_list))
 def test_filtered_fallback_results_still_try_indexer(self):
  self.configure();bad=listing(title='Example.2020.2160p.AV1')
  with patch.object(app,'mediafusion_streams',return_value=[{'infoHash':'b'*40,'title':'2160p AV1'}]),patch.object(app,'fetch',return_value=listing()):
   self.assertEqual(app.fallback_candidates('movie:tt0000001')[0]['_provider'],'jackett')
  self.assertEqual(app.filtered_streams({'streams':torznab_streams(bad,'movie:tt0000001')}),[])
 def test_indexer_fault_is_deferred_and_not_cached_as_empty(self):
  self.configure();key='movie:tt0000001'
  with patch.object(app,'mediafusion_streams',return_value=[]),patch.object(app,'fetch',return_value=b'<error code="900"/>'):
   with self.assertRaises(app.Checkpoint):app.fallback_candidates(key)
  self.assertIsNone(app.cached('jackett:'+key));self.assertIsNone(app.cached('source-status:'+key))
 def test_repeated_cooldown_never_enters_network(self):
  self.configure();app.remember('jackett-yts-control',{'cooldown':time.time()+600},90000)
  with patch.object(app,'fetch',side_effect=AssertionError('must not request')):
   with self.assertRaises(app.SourceProviderWait):app.jackett_streams('movie:tt0000001')
  self.assertFalse((app.DATA/'jackett-yts.lock.waiters').exists())
if __name__=='__main__':unittest.main()
