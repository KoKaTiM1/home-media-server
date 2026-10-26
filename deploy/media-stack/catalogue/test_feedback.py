import json,time,unittest
from unittest.mock import patch
import test_features as fixtures
a=fixtures.a
class FeedbackTests(unittest.TestCase):
 setUp=fixtures.FeatureTests.setUp
 def test_same_imdb_renamed_movie_matches_current_index_title_but_wrong_year_does_not(self):
  key='movie:tt28102562'
  with a.F.db() as c:
   c.execute('INSERT INTO items VALUES (?,?,?,?,?)',(key,'movie','tt28102562',json.dumps({'id':'tt28102562','name':'Vampires of Vijay Nagar','releaseInfo':'2025'}),'[]'))
   c.execute('INSERT INTO home_titles(imdb,kind,name,original,year) VALUES (?,?,?,?,?)',('tt28102562','movie','Thamma','Thamma',2025))
  self.assertTrue(a.candidate_matches({'title':'Thamma.2025.1080p.H264.AAC'},key))
  self.assertTrue(a.candidate_matches({'title':'Vampires.of.Vijay.Nagar.2025.1080p'},key))
  self.assertFalse(a.candidate_matches({'title':'Thamma.2024.1080p'},key))
  self.assertFalse(a.candidate_matches({'title':'Other.Movie.2025.1080p'},key))
 def stub(self):
  imdb='tt38732131';folder=a.LIBRARY/'shows'/imdb;folder.mkdir(parents=True)
  (folder/'poster.jpg').write_bytes(b'unchanged poster')
  meta={'id':imdb,'type':'series','name':'Money Trap','releaseInfo':'2026','videos':[
   {'season':1,'episode':1,'released':'2020-01-01','title':'First'},
   {'season':1,'episode':2,'released':'2020-01-02','title':'Second'},
   {'season':2,'episode':1,'released':'2099-01-01','title':'Future'}]}
  a.remember('meta:series:'+imdb,meta,3600)
  a.F.validation_result('series:'+imdb+':1:1',True,proof={'version':4})
  return imdb,folder,meta
 def test_cached_series_stub_repaired_without_marking_unchecked_episodes_ready(self):
  imdb,folder,meta=self.stub();calls=[]
  def api(path,*args,**kwargs):
   calls.append((path,args,kwargs))
   return {'Items':[{'Id':'native-series','Path':'/catalogue/shows/'+imdb}]} if path.startswith('/Items?') else None
  with patch.object(a,'make_poster'),patch.object(a.F,'jf',side_effect=api):
   self.assertEqual(a.F.repair_cached_series(only=imdb),[imdb])
  self.assertIn('Money Trap',(folder/'tvshow.nfo').read_text())
  self.assertTrue((folder/'Season 01/S01E01.strm').is_file());self.assertTrue((folder/'Season 01/S01E02.strm').is_file())
  self.assertFalse((folder/'Season 02').exists())
  self.assertEqual((folder/'poster.jpg').read_bytes(),b'unchanged poster')
  self.assertEqual(a.F.source_state('series:'+imdb+':1:1'),'verified')
  self.assertNotIn('series:'+imdb+':1:2',a.F.ready_keys())
  self.assertTrue(any(c[0]=='/Library/Media/Updated' and c[1][0]['Updates'][0]['Path']=='/catalogue/shows/'+imdb for c in calls))
  self.assertFalse(any('UserData' in c[0] and not 'EnableUserData=false' in c[0] for c in calls))
 def test_failed_refresh_retries_without_overwriting_strm_or_state(self):
  imdb,folder,meta=self.stub()
  with patch.object(a,'make_poster'),patch.object(a.F,'jf',side_effect=TimeoutError):a.F.repair_cached_series(only=imdb)
  stream=folder/'Season 01/S01E01.strm';stream.write_text('existing-stream')
  with patch.object(a,'import_item',side_effect=AssertionError('Do not reimport persisted series')),patch.object(a.F,'jf',side_effect=lambda path,*args,**kw: {'Items':[{'Id':'native-series','Path':'/catalogue/shows/'+imdb}]} if path.startswith('/Items?') else None):
   self.assertEqual(a.F.repair_cached_series(only=imdb),[imdb])
  self.assertEqual(stream.read_text(),'existing-stream')
 def test_stub_title_update_uses_supported_api_and_excludes_user_state(self):
  import sqlite3
  path=a.F.media.parent/'appdata/jellyfin/config/data/jellyfin.db';path.parent.mkdir(parents=True)
  with sqlite3.connect(path) as c:
   c.execute('CREATE TABLE BaseItems(Id TEXT,Name TEXT,Path TEXT,Type TEXT)')
   c.execute('INSERT INTO BaseItems VALUES (?,?,?,?)',('native-id','tt38732131','/catalogue/shows/tt38732131','MediaBrowser.Controller.Entities.TV.Series'))
  posted=[]
  def api(path,*args,**kwargs):
   if path=='/Users':return [{'Id':'owner','Policy':{'IsAdministrator':True}}]
   if path.startswith('/Users/'):return {'Name':'tt38732131','Tags':['existing'],'CommunityRating':7.5,'UserData':{'Played':False,'PlaybackPositionTicks':123},'ProviderIds':{'Tvdb':'old'}}
   posted.append((path,args[0]));return None
  with patch.object(a.F,'jf',side_effect=api):a.F.repair_stub_title('tt38732131',{'name':'Money Trap','releaseInfo':'2026'})
  self.assertEqual(posted[0][0],'/Items/native-id');dto=posted[0][1]
  self.assertEqual(dto['Name'],'Money Trap');self.assertEqual(dto['Tags'],['existing']);self.assertEqual(dto['CommunityRating'],7.5)
  self.assertNotIn('UserData',dto);self.assertEqual(dto['ProviderIds']['Tvdb'],'old')
  with sqlite3.connect(path) as c:self.assertEqual(c.execute('SELECT Name FROM BaseItems').fetchone()[0],'tt38732131')
 def test_no_repair_for_unready_or_navigation_or_missing_cache(self):
  imdb,folder,meta=self.stub()
  with a.F.db() as c:c.execute('DELETE FROM validation')
  with patch.object(a,'import_item') as imported:a.F.repair_cached_series(only=imdb);imported.assert_not_called()
  with patch.object(a.F,'foreground_busy',return_value=True),patch.object(a.F,'jf') as native:
   a.F.repair_cached_series(only=imdb);native.assert_not_called()
if __name__=='__main__':unittest.main()
