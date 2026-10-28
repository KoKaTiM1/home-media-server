import importlib.util
import json
from pathlib import Path
import sqlite3
import tempfile
import time
import unittest
from urllib.parse import parse_qs, urlsplit
from unittest.mock import patch

spec=importlib.util.spec_from_file_location('feature_test_app',Path(__file__).with_name('app.py'))
a=importlib.util.module_from_spec(spec);spec.loader.exec_module(a)


class FeatureTests(unittest.TestCase):
    def test_preview_episode_count_uses_local_released_distinct_episodes_without_network(self):
        import datetime
        past=(datetime.date.today()-datetime.timedelta(days=1)).isoformat()
        future=(datetime.date.today()+datetime.timedelta(days=1)).isoformat()
        metadata={'videos':[
            {'season':1,'episode':1,'released':past},
            {'season':1,'episode':1,'released':past},
            {'season':0,'episode':1,'released':past},
            {'season':1,'episode':2,'released':future},
            {'season':1,'episode':3},
            {'season':1,'episode':0,'released':past},
            {'season':'invalid','episode':4,'released':past}]}
        with a.F.db() as conn:
            conn.execute('INSERT INTO items VALUES (?,?,?,?,?)',
                         ('series:tt1234567','series','tt1234567',json.dumps(metadata),'[]'))
        with patch.object(a,'fetch',side_effect=AssertionError('Browsing must not fetch episode metadata')):
            result=a.F.decorate_posters([{'id':'tt1234567','type':'series','name':'Fixture'}])
        self.assertEqual(result[0]['episodeCount'],2)
        self.assertIn('/poster/series/',result[0]['poster'])
        self.assertNotIn('tt7654321',a.F.series_episode_counts(['tt7654321']))

    def test_episode_count_prefers_saved_cache_and_survives_cache_expiry(self):
        import datetime
        past=(datetime.date.today()-datetime.timedelta(days=1)).isoformat()
        original={'videos':[{'season':1,'episode':1,'released':past}]}
        with a.F.db() as conn:
            conn.execute('INSERT INTO items VALUES (?,?,?,?,?)',
                         ('series:tt1234567','series','tt1234567',json.dumps(original),'[]'))
        updated={'videos':[{'season':1,'episode':i,'released':past} for i in (1,2,3)]}
        a.remember('meta:series:tt1234567',updated,-1)
        self.assertEqual(a.F.series_episode_counts(['tt1234567']),{'tt1234567':3})

    def setUp(self):
        self.directory=tempfile.TemporaryDirectory();self.addCleanup(self.directory.cleanup)
        self.root=Path(self.directory.name)
        self.patch=patch.multiple(a,DATA=self.root/'data',LIBRARY=self.root/'catalogue',
            MEDIA_ROOT=self.root/'media',DOWNLOADS_ROOT=self.root/'downloads',create=True)
        self.patch.start();self.addCleanup(self.patch.stop)
        a.F.media.mkdir();a.F.downloads.mkdir();a.F.db().close()

    def verify_fixture(self,path):
        with sqlite3.connect(path) as conn:rows=conn.execute('SELECT kind,imdb FROM titles').fetchall()
        with a.F.db() as conn:
            conn.executemany("INSERT INTO validation(key,state,checked,proof) VALUES (?,'verified',?,?)",((kind+':'+imdb,time.time(),json.dumps({'version':2})) for kind,imdb in rows))

    def test_localized_title_and_description_fallback_independently(self):
        with a.F.db() as conn:
            conn.execute('INSERT INTO aliases VALUES (?,?,?)',('tt1234567','שם בעברית','English title'))
            conn.execute('INSERT INTO localized_metadata(kind,imdb,title_he,description_he,state) VALUES (?,?,?,?,?)',
                         ('movie','tt1234567','שם בעברית','תיאור בעברית','complete'))
        complete=a.F.localize({'id':'tt1234567','type':'movie','name':'English title','description':'English overview'})
        self.assertEqual((complete['name'],complete['description'],complete['englishName']),
                         ('שם בעברית','תיאור בעברית','English title'))
        with a.F.db() as conn:conn.execute('UPDATE localized_metadata SET description_he=NULL WHERE imdb=?',('tt1234567',))
        title_only=a.F.localize({'id':'tt1234567','type':'movie','name':'English title','description':'English overview'})
        self.assertEqual(title_only['name'],'שם בעברית')
        self.assertEqual(title_only['description'],'English overview')
        with a.F.db() as conn:
            conn.execute('DELETE FROM aliases WHERE imdb=?',('tt1234568',))
            conn.execute('INSERT INTO localized_metadata(kind,imdb,description_he,state) VALUES (?,?,?,?)',
                         ('series','tt1234568','תיאור בעברית','complete'))
        description_only=a.F.localize({'id':'tt1234568','type':'series','name':'English show','description':'English overview'})
        self.assertEqual(description_only['name'],'English show')
        self.assertEqual(description_only['description'],'תיאור בעברית')

    def test_wikidata_requires_exact_imdb_and_uses_hebrew_article_summary(self):
        wrong={'claims':{'P345':[{'mainsnak':{'datavalue':{'value':'tt7654321'}}}]},'labels':{'he':{'value':'לא נכון'}}}
        right={'claims':{'P345':[{'mainsnak':{'datavalue':{'value':'tt1234567'}}}]},
               'labels':{'he':{'value':'שם נכון'}},'descriptions':{'he':{'value':'תיאור קצר'}},
               'sitelinks':{'hewiki':{'title':'שם נכון (סרט)'}}}
        with patch.object(a.F,'wikimedia_json',side_effect=[{'search':[{'id':'Q1'},{'id':'Q2'}]},
                {'entities':{'Q1':wrong,'Q2':right}},{'extract':'תקציר עברי ארוך'}]) as fetcher:
            result=a.F.wikidata_title_metadata('movie','tt1234567','English title')
        self.assertEqual(result,{'title_he':'שם נכון','description_he':'תקציר עברי ארוך','source':'wikidata+hewiki'})
        self.assertEqual(fetcher.call_count,3)

    def test_nfo_localization_preserves_identity_and_other_metadata(self):
        folder=a.LIBRARY/'movies'/'tt1234567';folder.mkdir(parents=True)
        path=folder/'movie.nfo'
        path.write_text('<movie><title>Old English</title><year>2001</year><uniqueid type="imdb">tt1234567</uniqueid><plot>Old plot</plot><trailer>keep-me</trailer></movie>')
        changed=a.F.apply_localized_nfo('movie','tt1234567',{'englishName':'English title'},'שם בעברית','תקציר בעברית')
        root=__import__('xml.etree.ElementTree',fromlist=['ElementTree']).parse(path).getroot()
        self.assertTrue(changed)
        self.assertEqual(root.findtext('title'),'שם בעברית')
        self.assertEqual(root.findtext('originaltitle'),'English title')
        self.assertEqual(root.findtext('plot'),'תקציר בעברית')
        self.assertEqual(root.findtext('uniqueid'),'tt1234567')
        self.assertEqual(root.findtext('trailer'),'keep-me')

    def test_provider_failure_keeps_existing_english_metadata_and_video_proof(self):
        key='movie:tt1234567';a.F.validation_result(key,True,proof={'version':5})
        with a.F.db() as conn:
            conn.execute('INSERT INTO items VALUES (?,?,?,?,?)',(key,'movie','tt1234567',
                         json.dumps({'id':'tt1234567','type':'movie','name':'English title','description':'English overview'}),'[]'))
        folder=a.LIBRARY/'movies'/'tt1234567';folder.mkdir(parents=True)
        (folder/'movie.nfo').write_text('<movie><title>English title</title><plot>English overview</plot></movie>')
        with patch.object(a.F,'jf',return_value=[]),patch.object(a.F,'wikidata_title_metadata',side_effect=TimeoutError('offline')):
            a.F.refresh_hebrew_metadata(limit=1)
        with a.F.db() as conn:
            state=conn.execute('SELECT state,next_attempt FROM localized_metadata WHERE kind=? AND imdb=?',('movie','tt1234567')).fetchone()
            proof=conn.execute('SELECT state,proof FROM validation WHERE key=?',(key,)).fetchone()
        self.assertEqual(state[0],'retry');self.assertGreater(state[1],time.time())
        self.assertEqual(proof[0],'verified');self.assertEqual(json.loads(proof[1])['version'],5)
        self.assertIn('English overview',(folder/'movie.nfo').read_text())

    def test_provider_failure_keeps_cached_hebrew_fields(self):
        key='movie:tt1234567';a.F.validation_result(key,True,proof={'version':5})
        with a.F.db() as conn:
            conn.execute('INSERT INTO items VALUES (?,?,?,?,?)',(key,'movie','tt1234567',
                         json.dumps({'id':'tt1234567','type':'movie','name':'English title','description':'English overview'}),'[]'))
            conn.execute('INSERT INTO aliases VALUES (?,?,?)',('tt1234567','שם שמור','English title'))
            conn.execute('INSERT INTO localized_metadata(kind,imdb,title_he,description_he,state,next_attempt) VALUES (?,?,?,?,?,0)',
                         ('movie','tt1234567','שם שמור','תיאור שמור','complete'))
        folder=a.LIBRARY/'movies'/'tt1234567';folder.mkdir(parents=True)
        (folder/'movie.nfo').write_text('<movie><title>שם שמור</title><plot>תיאור שמור</plot></movie>')
        with patch.object(a.F,'jf',return_value=[]),patch.object(a.F,'wikidata_title_metadata',side_effect=TimeoutError('offline')):
            a.F.refresh_hebrew_metadata(limit=1)
        localized=a.F.localize({'id':'tt1234567','type':'movie','name':'English title','description':'English overview'})
        self.assertEqual((localized['name'],localized['description']),('שם שמור','תיאור שמור'))

    def test_unmatched_wikidata_entity_keeps_english_fallback(self):
        entity={'claims':{'P345':[{'mainsnak':{'datavalue':{'value':'tt7654321'}}}]}}
        with patch.object(a.F,'wikimedia_json',side_effect=[{'search':[{'id':'Q1'}]},{'entities':{'Q1':entity}}]):
            self.assertIsNone(a.F.wikidata_title_metadata('movie','tt1234567','English title'))
        self.assertEqual(a.F.localize({'id':'tt1234567','type':'movie','name':'English title',
                                      'description':'English overview'}),
                         {'id':'tt1234567','type':'movie','name':'English title','description':'English overview'})

    def test_audio_resync_retains_verified_visibility_and_only_migrates_once(self):
        good='movie:tt1234567';failed='movie:tt2345678'
        a.F.validation_result(good,True,proof={'version':2})
        a.F.validation_result(failed,False)
        a.F.schedule_audio_resync()
        self.assertEqual(a.F.source_state(good),'verified')
        with a.F.db() as conn:
            self.assertEqual(conn.execute('SELECT completed FROM audio_recheck WHERE key=?',(good,)).fetchone()[0],0)
            retry=conn.execute('SELECT next_check,attempts FROM validation WHERE key=?',(failed,)).fetchone()
            self.assertLessEqual(retry[0],time.time());self.assertEqual(retry[1],1)
        a.F.validation_result(failed,False)
        with a.F.db() as conn:later=conn.execute('SELECT next_check FROM validation WHERE key=?',(failed,)).fetchone()[0]
        a.F.schedule_audio_resync()
        with a.F.db() as conn:self.assertEqual(conn.execute('SELECT next_check FROM validation WHERE key=?',(failed,)).fetchone()[0],later)
        a.F.validation_result(good,True,proof={'audio_policy':1})
        with a.F.db() as conn:self.assertGreater(conn.execute('SELECT completed FROM audio_recheck WHERE key=?',(good,)).fetchone()[0],0)

    def test_first_pass_prefers_new_titles_over_more_episodes(self):
        self.make_search_index()
        movie='movie:tt1234567';episode='series:tt2345678:7:18'
        a.F.enqueue(movie,20);a.F.enqueue(episode,100)
        with patch.object(a.F,'jf',return_value=[]),patch.object(a,'prepared',return_value={}) as prepare,patch('features.time.sleep'),patch.object(a.F,'worker_audit',return_value=None):
            a.F.warm_availability(1)
            prepare.assert_called_once_with(movie,background=True,cancel=unittest.mock.ANY)

    def test_due_failure_gets_a_retry_ahead_of_large_pending_backlog(self):
        self.make_search_index()
        failed='movie:tt1234567';new='movie:tt2345678'
        a.F.validation_result(failed,False)
        a.F.enqueue(new,100)
        with a.F.db() as conn:conn.execute('UPDATE validation SET next_check=0 WHERE key=?',(failed,))
        with patch.object(a.F,'jf',return_value=[]),patch.object(a,'prepared',return_value={}) as prepare,patch('features.time.sleep'),patch.object(a.F,'worker_audit',return_value=None):
            a.F.warm_availability(1)
            prepare.assert_called_once_with(failed,background=True,cancel=unittest.mock.ANY)

    def test_provider_rate_limit_pauses_the_queue(self):
        import urllib.error
        self.make_search_index()
        key='movie:tt1234567';a.F.enqueue(key,100)
        error=urllib.error.HTTPError('https://example.invalid',429,'Rate limited',{},None)
        with patch.object(a.F,'jf',return_value=[]),patch.object(a,'prepared',side_effect=error) as prepare:
            a.F.warm_availability(1)
            self.assertTrue(a.cached('validation-provider-backoff'))
            a.F.warm_availability(1)
            self.assertEqual(prepare.call_count,1)
        self.assertEqual(a.F.source_state(key),'pending')

    def test_null_release_date_search_queues_once_and_caches_provider_response(self):
        self.make_search_index()
        meta={'id':'tt1234567','type':'movie','name':'Unknown release','released':None}
        with patch.object(a.F,'fetch',return_value={'metas':[meta]}) as fetch:
            self.assertEqual(a.F.search('movie','Unknown release'),[])
            self.assertEqual(a.F.search('movie','Unknown release'),[])
            self.assertEqual(fetch.call_count,1)
        with a.F.db() as conn:
            self.assertEqual(conn.execute('SELECT state,priority FROM validation WHERE key=?',('movie:tt1234567',)).fetchone(),('pending',100))
            self.assertIsNotNone(conn.execute('SELECT meta FROM discovery WHERE imdb=?',('tt1234567',)).fetchone())

    def test_durable_failures_backoff_and_keep_old_proof(self):
        key='movie:tt1234567'
        a.F.validation_result(key,True,proof={'source_hash':'a'*40})
        a.remember('prepared:'+key,{'stale':True},300)
        a.F.validation_result(key,False,reason='Network timeout')
        self.assertEqual(a.F.source_state(key),'unavailable')
        self.assertIsNone(a.cached('prepared:'+key))
        with a.F.db() as conn:first=conn.execute('SELECT attempts,next_check,proof FROM validation WHERE key=?',(key,)).fetchone()
        self.assertEqual(first[0],1);self.assertIn('source_hash',first[2])
        a.F.enqueue(key,100)
        with a.F.db() as conn:self.assertEqual(conn.execute('SELECT next_check FROM validation WHERE key=?',(key,)).fetchone()[0],first[1])
        a.F.validation_result(key,False,reason='Still unavailable')
        with a.F.db() as conn:second=conn.execute('SELECT attempts,next_check FROM validation WHERE key=?',(key,)).fetchone()
        self.assertEqual(second[0],2);self.assertGreater(second[1]-first[1],21000)
        a.F.validation_result(key,True,proof={'source_hash':'b'*40})
        self.assertEqual(a.F.source_state(key),'verified')
        with a.F.db() as conn:self.assertEqual(conn.execute('SELECT attempts FROM validation WHERE key=?',(key,)).fetchone()[0],0)

    def test_unknown_and_listed_hidden_failure_overrides_old_success(self):
        path=self.make_search_index()
        key='movie:tt0145487'
        with a.F.db() as conn:conn.execute('DELETE FROM validation WHERE key=?',(key,))
        a.remember('streams:'+key,[{'infoHash':'a'*40}],3600)
        self.assertEqual(a.F.source_state(key),'unknown')
        self.assertNotIn(key,a.F.ready_titles())
        self.assertNotIn('tt0145487',{m['id'] for m in a.F.browse('movie')['metas']})
        a.F.validation_result(key,True,proof={'version':2})
        a.remember('source-status:'+key,{'state':'unavailable','checked_at':time.time()+1},3600)
        self.assertEqual(a.F.source_state(key),'unavailable')
        self.assertNotIn(key,a.F.ready_titles())

    def test_queue_resumes_without_resetting_prior_results_and_defers_playback(self):
        self.make_search_index()
        key='movie:tt0145487'
        a.F.validation_result(key,False)
        with a.F.db() as conn:before=conn.execute('SELECT state,attempts,next_check,proof FROM validation WHERE key=?',(key,)).fetchone()
        a.F.seed_validation();a.F.seed_validation()
        with a.F.db() as conn:after=conn.execute('SELECT state,attempts,next_check,proof FROM validation WHERE key=?',(key,)).fetchone()
        self.assertEqual(before,after)
        with patch.object(a.F,'jf',return_value=[{'NowPlayingItem':{'Id':'playing'}}]),patch.object(a,'prepared') as prepare:
            a.F.warm_availability(1)
            prepare.assert_not_called()

    def test_catalogue_pages_beyond_old_cap_and_year_and_hidden_sources(self):
        path=a.DATA/'imdb/catalogue.sqlite';path.parent.mkdir()
        conn=sqlite3.connect(path)
        conn.execute('CREATE TABLE titles(imdb TEXT PRIMARY KEY,kind TEXT,name TEXT,original TEXT,year INT,genres TEXT,votes INT)')
        conn.executemany('INSERT INTO titles VALUES (?,?,?,?,?,?,?)',[
            (f'tt{1000000+i}','movie',f'Title {i}','Original',2025,'Drama',2000-i) for i in range(1100)])
        conn.execute('INSERT INTO titles VALUES (?,?,?,?,?,?,?)',('tt9999999','movie','Earlier','Earlier',2022,'Drama',9))
        conn.commit();conn.close()
        self.verify_fixture(path)
        a.remember('source-status:movie:tt1000000',{'state':'unavailable','checked_at':time.time()+1},3600)
        first=a.F.browse('movie',0,1000,2025);second=a.F.browse('movie',1000,1000,2025)
        self.assertEqual(len(first['metas']),1000);self.assertEqual(len(second['metas']),99)
        self.assertEqual(first['total'],1099)
        self.assertFalse({m['id'] for m in first['metas']}&{m['id'] for m in second['metas']})
        self.assertTrue(all(m['releaseInfo']=='2025' for m in first['metas']+second['metas']))
        self.assertEqual(a.F.browse('movie',0,100,2022)['metas'][0]['name'],'Earlier')
        a.F.ensure_search_index(path)
        with a.F.db() as labels:labels.execute('INSERT INTO aliases VALUES (?,?,?)',('tt1000001','סרט לדוגמה','Title 1'))
        self.assertEqual(a.F.index_search('movie','לדוגמה')[0]['id'],'tt1000001')
        self.assertTrue(a.F.index_search('movie','Title'))

    def make_search_index(self):
        path=a.DATA/'imdb/catalogue.sqlite';path.parent.mkdir()
        with sqlite3.connect(path) as conn:
            conn.execute('CREATE TABLE titles(imdb TEXT PRIMARY KEY,kind TEXT,name TEXT,original TEXT,year INT,genres TEXT,votes INT,rating REAL,runtime INT)')
            conn.executemany('INSERT INTO titles VALUES (?,?,?,?,?,?,?,?,?)',[
                ('tt0145487','movie','Spider-Man','Spider-Man',2002,'Action',500,7.4,121),
                ('tt0316654','movie','Spider-Man 2','Spider-Man 2',2004,'Action',200,7.5,127),
                ('tt0000003','movie','Zebra','Zebra',2004,'Drama',900,6.0,100),
                ('tt0000004','movie','Earlier','Earlier',2000,'Drama',900,9.0,100),
                ('tt0000005','movie','Old','Old',1994,'Drama',900,9.5,100),
                ('tt0000006','movie','Too old','Too old',1993,'Drama',900,9.5,100)])
        conn.close()
        self.verify_fixture(path)
        a.F.ensure_search_index(path)
        return path

    def test_secondary_sort_preserves_year_and_applies_before_paging(self):
        self.make_search_index()
        high=a.F.browse('movie',0,2,'2000-2004','CommunityRating','Descending')
        low=a.F.browse('movie',0,2,'2000-2004','CommunityRating','Ascending')
        self.assertEqual([m['id'] for m in high['metas']],['tt0316654','tt0000003'])
        self.assertEqual([m['id'] for m in low['metas']],['tt0000003','tt0316654'])
        self.assertEqual(a.F.browse('movie',2,1,'2000-2004','CommunityRating')['metas'][0]['id'],'tt0145487')
        self.assertEqual({m['releaseInfo'] for m in a.F.browse('movie',0,100,'1994-2000')['metas']},{'1994','2000'})
        self.assertEqual(a.F.parse_years('1994,2000'),[2000,1994])
        for invalid in ['2000-1994','1900','invalid']:
            with self.assertRaises(ValueError):a.F.parse_years(invalid)

    def test_hebrew_franchise_search_expands_to_english_movie_titles(self):
        self.make_search_index()
        responses=[{'search':[{'id':'Q123'}]}, {'entities':{'Q123':{'claims':{},'labels':{
            'he':{'value':'ספיידרמן'},'en':{'value':'Spider-Man (character)'}}}}}]
        with patch.object(a.F,'fetch',side_effect=responses):
            result=a.F.search('movie','ספיידרמן')
        self.assertEqual({m['id'] for m in result},{'tt0145487','tt0316654'})
        self.assertEqual(a.F.search_key('ספיידר-מן'),a.F.search_key('ספיידרמן'))
        with a.F.db() as conn:
            original=json.loads(conn.execute('SELECT meta FROM discovery WHERE imdb=?',('tt0145487',)).fetchone()[0])
        self.assertTrue(original['poster'].startswith('https://'))
        self.assertTrue(all('/poster/movie/' in m['poster'] for m in result))

    def test_poster_badge_tracks_confirmed_hebrew_and_unknown(self):
        self.assertEqual(a.poster_status('movie','tt0145487'),'?')
        a.remember('wizdom-search:movie:tt0145487',[{'id':123}],3600)
        self.assertEqual(a.poster_status('movie','tt0145487'),'HE')
        preview={'id':'tt0145487','type':'movie','poster':'https://example.org/poster.jpg'}
        result=a.F.decorate_posters([preview])
        self.assertEqual(parse_qs(urlsplit(result[0]['poster']).query)['badge'],['HE'])
        self.assertEqual(result[0]['poster'],a.F.poster_url('movie','tt0145487'))
        self.assertEqual(a.poster_status('movie','tt0316654'),'?')

    def test_real_release_month_sort_filter_and_unknown_dates(self):
        path=self.make_search_index()
        with sqlite3.connect(path) as conn:conn.execute('ALTER TABLE titles ADD COLUMN released TEXT')
        conn.close()
        a.F.record_dates('movie',[
            {'id':'tt0316654','released':'2004-07-30T00:00:00Z'},
            {'id':'tt0000003','released':'2004-07-02T00:00:00Z'},
            {'id':'tt0145487','released':'2002-05-03T00:00:00Z'},
            {'id':'tt0000004','released':'2000'},
        ])
        a.F.ensure_browse_indexes(path)
        result=a.F.browse('movie',0,100,month='2004-07')
        self.assertEqual([m['id'] for m in result['metas']],['tt0316654','tt0000003'])
        self.assertEqual([m['released'][:10] for m in result['metas']],['2004-07-30','2004-07-02'])
        self.assertEqual(a.F.search('movie','2004-07')[0]['id'],'tt0316654')
        self.assertEqual(a.F.browse('movie',0,100,sort_by='DateCreated',month='2004-07')['metas'][0]['id'],'tt0316654')
        self.assertEqual(a.F.parse_months('2004-12..2005-02'),('2004-12-01','2005-03-01'))
        with sqlite3.connect(path) as conn:
            self.assertIsNone(conn.execute('SELECT released FROM titles WHERE imdb=?',('tt0000004',)).fetchone()[0])
            conn.execute('UPDATE titles SET released=NULL')
        conn.close()
        a.F.apply_dates(path)
        self.assertEqual(a.F.browse('movie',0,100,month='2004-07')['total'],2)
        for invalid in ['2004-00','2004-13','2004-08..2004-07']:
            with self.assertRaises(ValueError):a.F.parse_months(invalid)

    def test_save_selects_only_requested_torrent_file(self):
        calls=[]
        def rpc(method,args):
            calls.append((method,args));return {'torrents':[]} if method=='torrent-get' else {'torrent-added':{'id':1}}
        selected={'hash':'a'*40,'index':2,'file_count':3,'filename':'Show.S01E02.mkv','length':100}
        with patch.object(a.F,'transmission',side_effect=rpc),patch('features.shutil.disk_usage',return_value=type('Usage',(),{'free':100*1024**3})()):
            record=a.F.queue_download('series:tt1234567:1:2',selected)
        added=next(args for method,args in calls if method=='torrent-add')
        self.assertEqual(added['files-wanted'],[1]);self.assertEqual(added['files-unwanted'],[0,2])
        self.assertEqual(record['state'],'queued')

    def test_existing_external_download_is_never_modified(self):
        calls=[]
        def rpc(method,args):calls.append(method);return {'torrents':[{'hashString':'b'*40}]}
        with patch.object(a.F,'transmission',side_effect=rpc),patch('features.shutil.disk_usage',return_value=type('Usage',(),{'free':100*1024**3})()):
            with self.assertRaises(RuntimeError):
                a.F.queue_download('movie:tt1234567',{'hash':'b'*40,'index':1,'file_count':1,'filename':'m.mp4','length':1})
        self.assertEqual(calls,['torrent-get'])

    def test_completed_download_moves_to_media_and_resolves_locally(self):
        source=a.F.downloads/'Saved/release/movie.mp4';source.parent.mkdir(parents=True);source.write_bytes(b'video-data')
        key='movie:tt1234567'
        a.F.save_record(key,{'state':'queued','hash':'c'*40,'index':0,'requested_at':time.time(),'length':10})
        def rpc(method,args):
            if method=='torrent-get':return {'torrents':[{'files':[{'name':'release/movie.mp4','length':10,'bytesCompleted':10}]}]}
            return {}
        with patch.object(a.F,'transmission',side_effect=rpc),patch.object(a,'metadata',return_value={'name':'Test','releaseInfo':'2025'}), \
             patch.object(a.F,'jf',return_value={}),patch.object(a.F,'cleanup_saved'):
            a.F.manage_downloads()
        record=a.F.records()[key];self.assertEqual(record['state'],'ready')
        self.assertEqual(Path(record['path']).read_bytes(),b'video-data');self.assertFalse(source.exists())
        with patch.object(a,'torr',side_effect=AssertionError('Local playback must not contact TorrServer')):
            self.assertEqual(a.resolve(key)['local'],record['path'])

    def make_ready(self):
        path=a.F.media/'movies/Saved/tt1234567/movie.mp4';path.parent.mkdir(parents=True);path.write_bytes(b'owned')
        record={'state':'ready','path':str(path),'size':5,'inode':path.stat().st_ino,'sidecars':[],
                'mtime_ns':path.stat().st_mtime_ns,'completed_at':time.time(),'requested_at':time.time()-10}
        a.F.save_record('movie:tt1234567',record)
        manual=a.F.media/'movies/my-existing-movie.mp4';manual.write_bytes(b'keep')
        return path,manual

    def fake_jf(self,active=False,recent=True):
        def api(path,*args,**kwargs):
            if path=='/Sessions':return [{'NowPlayingItem':{'Id':'movie-id'}}] if active else []
            if path=='/Users':return [{'Id':'user'}]
            if path.startswith('/Items?'):
                return {'Items':[{'Id':'movie-id','Type':'Movie','ProviderIds':{'Imdb':'tt1234567'}}], 'TotalRecordCount':1}
            if path.startswith('/UserItems/'):
                date=a.dt.datetime.now(a.dt.timezone.utc) if recent else a.dt.datetime(2000,1,1,tzinfo=a.dt.timezone.utc)
                return {'Played':True,'LastPlayedDate':date.isoformat()}
            return None
        return api

    def test_cleanup_keeps_active_playback_then_removes_only_owned_download(self):
        saved,manual=self.make_ready()
        with patch.object(a.F,'jf',side_effect=self.fake_jf(active=True)):a.F.cleanup_saved()
        self.assertTrue(saved.exists())
        with patch.object(a.F,'jf',side_effect=self.fake_jf()):a.F.cleanup_saved()
        self.assertFalse(saved.exists());self.assertTrue(manual.exists())
        self.assertEqual(a.F.records()['movie:tt1234567']['state'],'removed')

    def test_historically_watched_title_is_not_removed_as_soon_as_download_finishes(self):
        saved,_=self.make_ready()
        with patch.object(a.F,'jf',side_effect=self.fake_jf(recent=False)):a.F.cleanup_saved()
        self.assertTrue(saved.exists())

    def test_replaced_managed_file_is_not_deleted(self):
        saved,_=self.make_ready();saved.write_bytes(b'user replacement')
        with patch.object(a.F,'jf',side_effect=self.fake_jf()):a.F.cleanup_saved()
        self.assertEqual(saved.read_bytes(),b'user replacement')

    def test_unrelated_watched_movie_cannot_trigger_saved_file_deletion(self):
        saved,_=self.make_ready();base=self.fake_jf()
        def api(path,*args,**kwargs):
            value=base(path,*args,**kwargs)
            if path.startswith('/Items?'):value['Items'][0]['ProviderIds']['Imdb']='tt9999999'
            return value
        with patch.object(a.F,'jf',side_effect=api):a.F.cleanup_saved()
        self.assertTrue(saved.exists())

    def test_move_recovers_after_restart_between_rename_and_ready_record(self):
        source=a.F.downloads/'Saved/release/movie.mp4';source.parent.mkdir(parents=True);source.write_bytes(b'video-data')
        key='movie:tt1234567'
        a.F.save_record(key,{'state':'queued','hash':'c'*40,'index':0,'requested_at':time.time(),'length':10})
        def rpc(method,args):
            if method=='torrent-get':return {'torrents':[{'files':[{'name':'release/movie.mp4','length':10,'bytesCompleted':10}]}]}
            return {}
        original=Path.rename
        def interrupted(path,target):original(path,target);raise RuntimeError('Simulated process restart')
        with patch.object(a.F,'transmission',side_effect=rpc),patch.object(a,'metadata',return_value={'name':'Test','releaseInfo':'2025'}), \
             patch.object(a.F,'jf',return_value={}),patch.object(a.F,'cleanup_saved'):
            with patch.object(Path,'rename',new=interrupted):
                with self.assertRaises(RuntimeError):a.F.manage_downloads()
            self.assertEqual(a.F.records()[key]['state'],'moving')
            a.F.manage_downloads()
        record=a.F.records()[key];self.assertEqual(record['state'],'ready')
        self.assertEqual(Path(record['path']).read_bytes(),b'video-data')


if __name__=='__main__':unittest.main()
