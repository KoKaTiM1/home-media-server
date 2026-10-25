import importlib.util
from pathlib import Path
import tempfile
import unittest
import io
import zipfile
from unittest.mock import patch

spec=importlib.util.spec_from_file_location('app',str(Path(__file__).with_name('app.py')))
a=importlib.util.module_from_spec(spec)
spec.loader.exec_module(a)


class CatalogueTests(unittest.TestCase):
    def test_collection_pages_visit_empty_filtered_pages_and_titles_beyond_2000(self):
        offsets=[]
        def page(path,timeout):
            self.assertEqual(timeout,60)
            query=a.U.parse_qs(a.U.urlsplit(path).query)
            offset=int(query['StartIndex'][0]); offsets.append(offset)
            self.assertEqual(query['Limit'],['100'])
            self.assertEqual(query['EnableUserData'],['false'])
            self.assertEqual(query['EnableTotalRecordCount'],['true' if offset==0 else 'false'])
            visible=[{'Id':'last-title'}] if offset==2100 else []
            return {'Items':visible,'TotalRecordCount':2101 if offset==0 else 0}
        with patch.object(a,'jf',side_effect=page):
            result=list(a.collection_items(Recursive='true',IncludeItemTypes='Movie,Series'))
        self.assertEqual(result,[{'Id':'last-title'}])
        self.assertIn(2100,offsets)
        self.assertLessEqual(len(offsets),24)

    def test_collections_add_only_missing_catalogue_members(self):
        import json
        with tempfile.TemporaryDirectory() as directory,patch.object(a,'DATA',Path(directory)):
            with a.db() as conn:
                conn.execute('INSERT INTO items VALUES (?,?,?,?,?)',
                             ('movie:tt1234567','movie','tt1234567','{}',json.dumps(['Popular'])))
            def items(**filters):
                if filters.get('IncludeItemTypes')=='BoxSet':
                    return iter([{'Id':'collection','Name':'Online Popular'}])
                if filters.get('ParentId'):
                    return iter([{'Id':'existing'}])
                return iter([
                    {'Id':identifier,'Type':'Movie','Path':path,'ProviderIds':{'Imdb':'tt1234567'}}
                    for identifier,path in [('existing','/catalogue/movies/a/movie.strm'),
                                             ('new','/catalogue/movies/b/movie.strm'),
                                             ('local','/media/movies/local.mp4')]])
            with patch.object(a,'collection_items',side_effect=items),patch.object(a,'jf') as jf:
                a.update_collections()
            jf.assert_called_once_with('/Collections/collection/Items?ids=new',{},method='POST')

    @unittest.skipUnless(importlib.util.find_spec('PIL'), 'Pillow is installed in the bridge image')
    def test_missing_artwork_uses_badged_placeholder_and_suppresses_retries(self):
        with tempfile.TemporaryDirectory() as directory:
            folder=Path(directory)
            with patch.object(a,'fetch',side_effect=OSError('Provider image missing')) as fetch, patch.object(a,'poster_status',return_value='HE'):
                for _ in range(2):a.make_poster(folder,{'name':'Example movie','poster':'https://example.org/missing.jpg'},'movie','tt1234567')
                self.assertEqual(fetch.call_count,1)
                self.assertTrue((folder/'poster.jpg').is_file())
                self.assertEqual((folder/'badge.txt').read_text(),'HE')


    @unittest.skipUnless(importlib.util.find_spec('PIL'), 'Pillow is installed in the bridge image')
    def test_concurrent_poster_rendering_never_loses_the_poster(self):
        from concurrent.futures import ThreadPoolExecutor
        from PIL import Image
        with tempfile.TemporaryDirectory() as directory:
            folder=Path(directory)
            Image.new('RGB',(360,540),'#233445').save(folder/'poster-original.jpg')
            with patch.object(a,'poster_status',return_value='HE'):
                with ThreadPoolExecutor(max_workers=4) as pool:
                    list(pool.map(lambda _:a.make_poster(folder,{},'movie','tt1234567'),range(16)))
            with Image.open(folder/'poster.jpg') as image:self.assertEqual(image.size,(360,540))
            self.assertFalse(list(folder.glob('poster-tmp-*')))
            self.assertTrue((folder/'poster.jpg').stat().st_mode & 0o004)

    def test_torrent_add_timeout_keeps_waiting_for_metadata(self):
        candidate={'infoHash':'a'*40,'fileIdx':0,'title':'720p'}
        ready={'file_stats':[{'id':1,'path':'Show.S01E01.mkv','length':1234}]}
        def rpc(payload,timeout=20):
            if payload['action']=='add':raise TimeoutError('Metadata still loading')
            return ready if rpc.added else {}
        def calls(payload,timeout=20):
            if payload['action']=='add':rpc.added=True
            return rpc(payload,timeout)
        rpc.added=False
        with patch.object(a.F,'local_download',return_value=None),patch.object(a,'cached',return_value=None), \
             patch.object(a,'stream_candidates',return_value=[candidate]),patch.object(a,'torr',side_effect=calls), \
             patch.object(a,'remember',side_effect=lambda key,value,ttl:value):
            selected=a.resolve('series:tt1234567:1:1',deadline=a.time.monotonic()+45)
        self.assertEqual(selected['index'],1)
        self.assertEqual(selected['filename'],'Show.S01E01.mkv')

    def test_dead_source_is_hidden_after_preparation_failure(self):
        with tempfile.TemporaryDirectory() as directory,patch.object(a,'DATA',Path(directory)), \
             patch.object(a,'resolve',side_effect=RuntimeError('No working source')):
            with self.assertRaises(RuntimeError):a.prepared('movie:tt1234567')
            self.assertEqual(a.cached('source-status:movie:tt1234567')['state'],'unavailable')

    def test_source_selection_excludes_zero_seeds_and_prefers_healthy_swarms(self):
        streams=[{'infoHash':'a'*40,'name':'1080p H264','title':'Movie 👤 0'},
                 {'infoHash':'b'*40,'name':'1080p H264','title':'Movie 👤 1'},
                 {'infoHash':'c'*40,'name':'720p H264','title':'Movie 👤 1000'}]
        with tempfile.TemporaryDirectory() as directory,patch.object(a,'DATA',Path(directory)), \
             patch.object(a,'fetch',return_value={'streams':streams}):
            result=a.stream_candidates('movie:tt1234567')
            self.assertEqual(result[0]['infoHash'],'c'*40)
            self.assertNotIn('a'*40,[s['infoHash'] for s in result])

    def test_identity_rejects_invalid_input(self):
        self.assertEqual(a.identity('series','tt1234567',2,3),'series:tt1234567:2:3')
        for args in [('movie','../../etc/passwd'),('series','tt1234567',-1,1),('series','tt1234567',1,0)]:
            with self.assertRaises(ValueError): a.identity(*args)

    def test_episode_selection_does_not_choose_largest_other_episode(self):
        files=[{'id':1,'path':'Show.S01E01.mkv','length':900},
               {'id':2,'path':'Show.S01E02.mkv','length':100},
               {'id':3,'path':'sample.mp4','length':3000}]
        self.assertEqual(a.choose_file(files,'series:tt1234567:1:2',0)['id'],2)
        with self.assertRaises(ValueError): a.choose_file(files,'series:tt1234567:2:1',0)
        with self.assertRaises(ValueError): a.choose_file(files,'movie:tt1234567')
        self.assertEqual(a.choose_file(files,'movie:tt1234567',0)['id'],1)

    def test_signature_is_bound_to_one_item(self):
        with tempfile.TemporaryDirectory() as d:
            secret=Path(d)/'secret'; secret.write_bytes(b'test-secret')
            with patch.object(a,'SECRET',secret):
                self.assertNotEqual(a.signature('movie:tt1234567'),a.signature('movie:tt1234568'))

    def test_import_is_metadata_only_and_preserves_existing_stream(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d)
            with patch.multiple(a,DATA=root/'data',LIBRARY=root/'catalogue'),patch.object(a,'make_poster'):
                meta={'id':'tt1234567','name':'Test Movie','releaseInfo':'2020','genres':['Drama'],
                      'trailerStreams':[{'ytId':'FVI84Dfx2-I'}]}
                a.import_item('movie',meta,'Popular')
                file=root/'catalogue/movies/tt1234567/movie.strm'
                self.assertEqual(file.read_text(),'dynamiclibrary://movie/tt1234567\n')
                root=a.ET.parse(file.with_suffix('.nfo')).getroot()
                self.assertEqual(root.findtext('trailer'),'plugin://plugin.video.youtube/play/?video_id=FVI84Dfx2-I')
                file.write_text('existing-stream')
                a.import_item('movie',meta,'Recent Releases')
                self.assertEqual(file.read_text(),'existing-stream')

    def test_audio_policy_prefers_lc_and_preserves_other_track_indexes(self):
        with tempfile.TemporaryDirectory() as directory:
            selected={'filename':'movie.mkv','hash':'a'*40,'index':1}
            info={'streams':[
                {'index':0,'codec_type':'video','codec_name':'h264','pix_fmt':'yuv420p'},
                {'index':1,'codec_type':'audio','codec_name':'aac','profile':'Main','tags':{'language':'eng'}},
                {'index':3,'codec_type':'audio','codec_name':'ac3','tags':{'language':'eng'}},
                {'index':2,'codec_type':'audio','codec_name':'aac','profile':'LC','tags':{'language':'eng'}}],
                'format':{'duration':'60'}}
            with patch.object(a,'DATA',Path(directory)),patch.object(a,'resolve',return_value=selected),patch.object(a,'probe',return_value=info),patch.object(a,'external_subtitles',return_value=[]):
                result=a.prepared('movie:tt1234567')
            audio=[s for s in result['MediaStreams'] if s['Type']=='Audio']
            self.assertEqual([s['Index'] for s in audio],[2,1,3])
            self.assertEqual(audio[0]['Profile'],'LC')
            self.assertEqual(result['audio_policy'],2)

    def test_main_only_audio_is_eligible_when_conversion_passes(self):
        with tempfile.TemporaryDirectory() as directory:
            selected={'filename':'movie.mkv','hash':'a'*40,'index':1}
            info={'streams':[{'index':0,'codec_type':'video','codec_name':'h264','pix_fmt':'yuv420p'},
                {'index':1,'codec_type':'audio','codec_name':'aac','profile':'Main'}],'format':{'duration':'60'}}
            with patch.object(a,'DATA',Path(directory)),patch.object(a,'resolve',return_value=selected),patch.object(a,'probe',return_value=info),patch.object(a,'conversion_preflight') as convert,patch.object(a,'external_subtitles',return_value=[]):
                result=a.prepared('movie:tt1234567')
                self.assertTrue(result['AudioConversionRequired'])
                self.assertEqual(result['DefaultAudioStreamIndex'],1)
                self.assertEqual(a.F.source_state('movie:tt1234567'),'verified')
                convert.assert_called_once()

    def test_failed_audio_conversion_does_not_mark_title_ready(self):
        with tempfile.TemporaryDirectory() as directory:
            selected={'filename':'movie.mkv','hash':'a'*40,'index':1}
            info={'streams':[{'index':0,'codec_type':'video','codec_name':'h264','pix_fmt':'yuv420p'},
                {'index':1,'codec_type':'audio','codec_name':'aac','profile':'Main'}],'format':{'duration':'60'}}
            with patch.object(a,'DATA',Path(directory)),patch.object(a,'resolve',return_value=selected),patch.object(a,'probe',return_value=info),patch.object(a,'conversion_preflight',side_effect=RuntimeError('Conversion failed')),patch.object(a,'stream_candidates',return_value=[]),patch.object(a,'torr',return_value={}):
                with self.assertRaises(RuntimeError):a.prepared('movie:tt1234567')
                self.assertEqual(a.F.source_state('movie:tt1234567'),'unavailable')

    def test_conversion_probe_rejects_wrong_output_profile(self):
        import json
        from types import SimpleNamespace
        with tempfile.TemporaryDirectory() as directory,patch.object(a,'DATA',Path(directory)):
            selected={'local':'fixture.mp4','hash':'a'*40,'length':100}
            info={'streams':[{'codec_type':'video','codec_name':'h264'}]}
            wrong={'streams':[{'codec_type':'video','codec_name':'h264'},{'codec_type':'audio','codec_name':'aac','profile':'Main'}]}
            results=[SimpleNamespace(returncode=0),SimpleNamespace(returncode=0,stdout=json.dumps(wrong).encode())]
            with patch.object(a.subprocess,'run',side_effect=results):
                with self.assertRaises(RuntimeError):a.conversion_preflight('movie:tt1234567',selected,info,1,a.time.monotonic()+30)
            self.assertIsNone(a.cached('audio-convert-v2:'+'a'*40+':1:100'))

    def test_converted_source_with_unknown_duration_is_not_advertised(self):
        with tempfile.TemporaryDirectory() as directory:
            selected={'filename':'movie.mkv','hash':'a'*40,'index':1}
            info={'streams':[{'index':0,'codec_type':'video','codec_name':'h264','pix_fmt':'yuv420p'},
                {'index':1,'codec_type':'audio','codec_name':'aac','profile':'Main'}],'format':{'duration':'0'}}
            with patch.object(a,'DATA',Path(directory)),patch.object(a,'resolve',return_value=selected),patch.object(a,'probe',return_value=info),patch.object(a,'conversion_preflight') as convert,patch.object(a,'stream_candidates',return_value=[]),patch.object(a,'torr',return_value={}):
                with self.assertRaises(RuntimeError):a.prepared('movie:tt1234567')
                convert.assert_not_called()
                self.assertEqual(a.F.source_state('movie:tt1234567'),'unavailable')

    def test_trailer_links_only_accept_youtube_identities_and_deduplicate(self):
        meta={'trailerStreams':[{'ytId':'FVI84Dfx2-I'},{'ytId':'invalid'}],
              'trailers':[{'source':'FVI84Dfx2-I'},
                          {'source':'https://youtu.be/VFkjBy2b50Q'},
                          {'source':'https://www.youtube.com/watch?v=nUEQNVV3Gfs'},
                          {'source':'https://example.invalid/watch?v=d0XTFAMmhrE'},
                          {'source':'javascript:alert(1)'}]}
        self.assertEqual(a.trailer_urls(meta),[
            'https://www.youtube.com/watch?v=FVI84Dfx2-I',
            'https://www.youtube.com/watch?v=VFkjBy2b50Q',
            'https://www.youtube.com/watch?v=nUEQNVV3Gfs'])

    def test_subtitle_preference_and_missing_languages(self):
        for languages,expected in [(['eng','heb'],'heb'),(['eng'],'eng'),([],None)]:
            with self.subTest(languages=languages),tempfile.TemporaryDirectory() as directory:
                tracks=[{'index':0,'codec_type':'video','codec_name':'h264','pix_fmt':'yuv420p'},
                        {'index':1,'codec_type':'audio','codec_name':'aac','profile':'LC'}]
                for index,lang in enumerate(languages,2):
                    tracks.append({'index':index,'codec_type':'subtitle','codec_name':'subrip','tags':{'language':lang}})
                selected={'filename':'movie.mp4','hash':'a'*40,'index':1}
                info={'streams':tracks,'format':{'duration':'60','bit_rate':'500000'}}
                with patch.object(a,'DATA',Path(directory)),patch.object(a,'resolve',return_value=selected), \
                     patch.object(a,'probe',return_value=info),patch.object(a,'external_subtitles',return_value=[]):
                    result=a.prepared('movie:tt1234567')
                    chosen=next((t['Language'] for t in result['MediaStreams'] if t['Index']==result['DefaultSubtitleStreamIndex']),None)
                    self.assertEqual(chosen,expected)

    def test_downloaded_hebrew_preferred_over_embedded_english(self):
        with tempfile.TemporaryDirectory() as directory:
            selected={'filename':'movie.mp4','hash':'b'*40,'index':1}
            info={'streams':[{'index':0,'codec_type':'video','codec_name':'h264','pix_fmt':'yuv420p'},
                {'index':1,'codec_type':'audio','codec_name':'aac','profile':'LC'},
                {'index':2,'codec_type':'subtitle','codec_name':'subrip','tags':{'language':'eng'}},
                {'index':3,'codec_type':'video','codec_name':'mjpeg','disposition':{'attached_pic':1}}],
                'format':{'duration':'60'}}
            secret=Path(directory)/'secret';secret.write_bytes(b'test-key')
            with patch.multiple(a,DATA=Path(directory)/'data',SECRET=secret), \
                 patch.object(a,'resolve',return_value=selected),patch.object(a,'probe',return_value=info), \
                 patch.object(a,'external_subtitles',return_value=[{'lang':'heb'}]):
                result=a.prepared('movie:tt1234567')
                chosen=next(t for t in result['MediaStreams'] if t['Index']==result['DefaultSubtitleStreamIndex'])
                self.assertEqual(chosen['Language'],'heb')
                self.assertTrue(chosen['IsExternal'])
                self.assertFalse(any(t['Codec']=='mjpeg' for t in result['MediaStreams']))

    def test_external_search_downloads_only_preferred_language(self):
        with tempfile.TemporaryDirectory() as directory:
            calls=[]
            def fake_fetch(url,data=None,headers=None,**kwargs):
                calls.append(url)
                if '/subtitles?' in url:
                    return {'data':[{'attributes':{'language':'he','release':'movie','files':[{'file_id':1}]}},
                                    {'attributes':{'language':'en','release':'movie','files':[{'file_id':2}]}}]}
                if url.endswith('/download'):
                    self.assertEqual(data['file_id'],1)
                    return {'link':'https://example.invalid/subtitle'}
                return '1\n00:00:01,000 --> 00:00:02,000\nשלום\n'.encode()
            with patch.object(a,'DATA',Path(directory)), \
                 patch.object(a,'subtitle_config',return_value={'api_key':'test-only'}), \
                 patch.object(a,'wizdom_subtitles',return_value=[]), \
                 patch.object(a,'fetch',side_effect=fake_fetch):
                subs=a.external_subtitles('movie:tt1234567','movie.mp4')
                self.assertEqual([s['lang'] for s in subs],['heb'])
                self.assertTrue(Path(subs[0]['path']).exists())
                self.assertEqual(len(calls),3)

    def test_wizdom_exact_episode_and_release_and_hebrew_encoding(self):
        with tempfile.TemporaryDirectory() as directory:
            calls=[]
            subtitle='1\n00:00:01,000 --> 00:00:02,000\nשלום\n'
            archive=io.BytesIO()
            with zipfile.ZipFile(archive,'w') as z:
                z.writestr('../../outside.srt',subtitle.encode('cp1255'))
            def fake_fetch(url,**kwargs):
                calls.append(url)
                if '/search?' in url:
                    self.assertIn('imdb=tt0903747&season=1&episode=2',url)
                    return [{'id':1,'versioname':'Other.release'},{'id':2,'versioname':'Show.S01E02.1080p.x264'}]
                self.assertTrue(url.endswith('/files/sub/2'))
                return archive.getvalue()
            with patch.object(a,'DATA',Path(directory)),patch.object(a,'fetch',side_effect=fake_fetch):
                subs=a.external_subtitles('series:tt0903747:1:2','Show.S01E02.1080p.x264.mkv')
                self.assertEqual(subs[0]['lang'],'heb')
                self.assertTrue(subs[0]['release_match'])
                self.assertEqual(Path(subs[0]['path']).read_text(encoding='utf-8'),subtitle)
                self.assertEqual(len(calls),2)
                self.assertFalse((Path(directory).parent/'outside.srt').exists())


if __name__=='__main__': unittest.main()
