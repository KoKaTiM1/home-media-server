"""Two Hebrew choices must remain distinct through caching and HTTP delivery."""
import io,json,threading,unittest,zipfile
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch
import urllib.request
import test_features as fixtures
a=fixtures.a
KEY='series:tt0903747:1:2'
SRT='1\n00:00:01,000 --> 00:00:02,000\nשלום\n'
ALT='1\n00:00:03,000 --> 00:00:04,000\nשלום אחר\n'

def archive(text):
    output=io.BytesIO()
    with zipfile.ZipFile(output,'w') as z:z.writestr('../../outside.srt',text.encode('cp1255'))
    return output.getvalue()

class MultiSubtitlesTests(unittest.TestCase):
    setUp=fixtures.FeatureTests.setUp

    def test_best_two_distinct_choices_cached_without_overwriting_legacy_track(self):
        folder=a.DATA/'subtitles'/a.hashlib.sha256(KEY.encode()).hexdigest()[:24]
        folder.mkdir(parents=True);(folder/'he.srt').write_text('old active track')
        matches=[{'id':1,'versioname':'Other'},{'id':2,'versioname':'Show.S01E02.1080p.x264'}]
        def fetch(url,**kwargs):return archive(SRT if url.endswith('/2') else ALT)
        with patch.object(a,'wizdom_search',return_value=matches),patch.object(a,'fetch',side_effect=fetch) as network:
            subs=a.wizdom_subtitles(KEY,'Show.S01E02.1080p.x264.mkv')
            self.assertEqual(len(subs),2);self.assertTrue(subs[0]['release_match'])
            self.assertNotEqual(subs[0]['variant'],subs[1]['variant'])
            self.assertNotEqual(subs[0]['path'],subs[1]['path'])
            self.assertEqual(Path(subs[0]['path']).read_text(),SRT)
            self.assertEqual(Path(subs[1]['path']).read_text(),ALT)
            self.assertEqual(a.wizdom_subtitles(KEY,'Show.S01E02.1080p.x264.mkv'),subs)
            self.assertEqual(network.call_count,2)
        self.assertEqual((folder/'he.srt').read_text(),'old active track')
        self.assertFalse((a.DATA/'outside.srt').exists())

    def test_duplicates_and_broken_release_do_not_crowd_out_good_second_choice(self):
        matches=[{'id':i,'versioname':'same'} for i in range(4)]
        def fetch(url,**kwargs):
            if url.endswith('/2'):raise a.urllib.error.URLError('provider unavailable')
            return archive(ALT if url.endswith('/3') else SRT)
        with patch.object(a,'wizdom_search',return_value=matches),patch.object(a,'fetch',side_effect=fetch):
            result=a.wizdom_subtitles(KEY,'same.mkv')
        self.assertEqual(len(result),2);self.assertNotEqual(result[0]['variant'],result[1]['variant'])
        # Cue numbering/encoding alone is not a second translation.
        self.assertEqual(a.store_subtitle(KEY,SRT.encode(),'heb')['variant'],
                         a.store_subtitle(KEY,SRT.replace('1\n','100\n',1).replace('\n','\r\n').encode(),'heb')['variant'])

    def test_one_available_version_is_not_fabricated_into_two(self):
        with patch.object(a,'wizdom_search',return_value=[{'id':1,'versioname':'same'}]),patch.object(a,'fetch',return_value=archive(SRT)):
            self.assertEqual(len(a.wizdom_subtitles(KEY,'same.mkv')),1)

    def test_online_tracks_use_delivered_subtitles_and_local_tracks_keep_embedded(self):
        for embedded,local in ((0,False),(1,False),(3,False),(1,True),(3,True)):
            with self.subTest(embedded=embedded,local=local):
                key='movie:tt'+str(1234567+embedded+(100 if local else 0))
                native=[{'index':0,'codec_type':'video','codec_name':'h264','pix_fmt':'yuv420p'},
                        {'index':1,'codec_type':'audio','codec_name':'aac','profile':'LC'}]
                native += [{'index':i+2,'codec_type':'subtitle','codec_name':'subrip','tags':{'language':'heb'}} for i in range(embedded)]
                selected={'filename':'fixture.mp4','hash':'a'*40,'index':1,'local':'/media/fixture.mp4' if local else None}
                externals=[{'lang':'heb','variant':'a'*16},{'lang':'heb','variant':'b'*16}]
                with patch.object(a,'signature',return_value='fixture'),patch.object(a.REL,'verify',return_value={'version':5}),patch.object(a,'resolve',return_value=selected),patch.object(a,'probe',return_value={'streams':native,'format':{'duration':'60'}}),patch.object(a,'external_subtitles',return_value=externals) as external,patch.object(a,'signed_path',return_value='http://example.invalid/sub?sig=fixture'):
                    info=a.prepared(key)
                he=[t for t in info['MediaStreams'] if t['Type']=='Subtitle' and t['Language']=='heb']
                self.assertEqual([t['Title'] for t in he],['עברית 1','עברית 2'])
                self.assertEqual(len({t['Index'] for t in info['MediaStreams']}),len(info['MediaStreams']))
                self.assertEqual(info['DefaultSubtitleStreamIndex'],he[0]['Index'])
                if not local:
                    self.assertTrue(all(t['IsExternal'] for t in he))
                    self.assertEqual(external.call_args.kwargs['max_hebrew'],2)
                    self.assertTrue(all(t['Index']>embedded+1 for t in he))
                elif embedded>=2:external.assert_not_called()
                else:self.assertEqual(external.call_args.kwargs['max_hebrew'],2-embedded)
                self.assertEqual(info['subtitle_policy'],3)

    def test_signed_variant_route_serves_each_content_and_preserves_legacy(self):
        one=a.store_subtitle(KEY,SRT.encode(),'heb');two=a.store_subtitle(KEY,ALT.encode(),'heb')
        folder=Path(one['path']).parent;(folder/'he.srt').write_text('legacy')
        server=ThreadingHTTPServer(('127.0.0.1',0),a.Handler)
        thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
        root=f'http://127.0.0.1:{server.server_port}/sub/{KEY}?sig=fixture&lang=heb'
        try:
            with patch.object(a,'signature',return_value='fixture'):
                for sub,text in ((one,SRT),(two,ALT)):
                    with urllib.request.urlopen(root+'&variant='+sub['variant'],timeout=2) as r:self.assertEqual(r.read().decode(),text)
                with urllib.request.urlopen(root,timeout=2) as r:self.assertEqual(r.read(),b'legacy')
                with self.assertRaises(a.urllib.error.HTTPError) as invalid:urllib.request.urlopen(root+'&variant=../../secret',timeout=2)
                self.assertEqual(invalid.exception.code,400)
                with self.assertRaises(a.urllib.error.HTTPError) as unsigned:urllib.request.urlopen(root.replace('sig=fixture','sig=bad')+'&variant='+one['variant'],timeout=2)
                self.assertEqual(unsigned.exception.code,401)
        finally:server.shutdown();server.server_close();thread.join()

if __name__=='__main__':unittest.main()
