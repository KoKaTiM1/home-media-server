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
    def setUp(self):
        self.directory=tempfile.TemporaryDirectory();self.addCleanup(self.directory.cleanup)
        self.root=Path(self.directory.name)
        self.patch=patch.multiple(a,DATA=self.root/'data',LIBRARY=self.root/'catalogue',
            MEDIA_ROOT=self.root/'media',DOWNLOADS_ROOT=self.root/'downloads',create=True)
        self.patch.start();self.addCleanup(self.patch.stop)
        a.F.media.mkdir();a.F.downloads.mkdir();a.F.db().close()

    def test_catalogue_pages_beyond_old_cap_and_year_and_hidden_sources(self):
        path=a.DATA/'imdb/catalogue.sqlite';path.parent.mkdir()
        conn=sqlite3.connect(path)
        conn.execute('CREATE TABLE titles(imdb TEXT PRIMARY KEY,kind TEXT,name TEXT,original TEXT,year INT,genres TEXT,votes INT)')
        conn.executemany('INSERT INTO titles VALUES (?,?,?,?,?,?,?)',[
            (f'tt{1000000+i}','movie',f'Title {i}','Original',2025,'Drama',2000-i) for i in range(1100)])
        conn.execute('INSERT INTO titles VALUES (?,?,?,?,?,?,?)',('tt9999999','movie','Earlier','Earlier',2022,'Drama',9))
        conn.commit();conn.close()
        a.remember('source-status:movie:tt1000000',{'state':'unavailable'},3600)
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
        self.assertEqual(a.poster_status('movie','tt0316654'),'?')

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
