"""Real embedded Hebrew, durable chunk resumption, source fencing and delivery."""
import copy,json,subprocess,tempfile,time,unittest
from pathlib import Path
from unittest.mock import patch
import test_features as fixtures
from embedded_subtitles import EmbeddedSubtitles,SubtitleCheckpoint,covered,cues

a=fixtures.a
KEY='series:tt1234567:1:1'
SRT='1\n00:00:01,000 --> 00:00:03,000\nשלום ראשון\n\n2\n00:00:09,000 --> 00:00:12,000\nגבול\n\n3\n00:00:15,000 --> 00:00:17,000\nאמצע\n\n4\n00:00:24,000 --> 00:00:27,000\nסוף\n'

class EmbeddedTests(unittest.TestCase):
    def setUp(self):
        fixtures.FeatureTests.setUp(self)
        self.worker=EmbeddedSubtitles(a.__dict__)
        self.subtitle=self.root/'in.srt';self.subtitle.write_text(SRT)
        self.media=self.root/'clip.mkv'
        subprocess.run(['ffmpeg','-nostdin','-v','error','-f','lavfi','-i','testsrc2=s=64x64:r=5:d=30','-i',str(self.subtitle),'-map','0:v','-map','1:s','-c:v','libx264','-threads','1','-g','10','-c:s','srt','-metadata:s:s:0','language=heb',str(self.media)],check=True,capture_output=True)
        self.info=json.loads(subprocess.check_output(['ffprobe','-v','error','-show_streams','-show_format','-of','json',str(self.media)]))
        self.selected={'hash':'a'*40,'index':1,'filename':'fixture.mkv','length':self.media.stat().st_size}
        self.sid=a.REL.sid(self.selected)
        a.F.validation_result(KEY,True,proof={'version':5,'source_id':self.sid})
        self.patches=[patch.object(a,'detail_playback_busy',return_value=False),patch.object(a.REL,'source_url',return_value=str(self.media)),patch.object(self.worker,'transport'),patch('embedded_subtitles.WINDOW',10)]
        for p in self.patches:p.start();self.addCleanup(p.stop)
    def row(self):
        with self.worker.db() as c:return c.execute('SELECT state,cursor,files FROM embedded_subtitles WHERE key=? AND sid=?',(KEY,self.sid)).fetchone()
    def seed(self):self.worker.enqueue(KEY,self.selected,30)
    def test_real_chunks_resume_without_publishing_partial_and_preserve_absolute_cues(self):
        self.seed()
        self.assertTrue(self.worker.once(time.monotonic()+15));self.assertEqual(self.row()[:2],('queued',10))
        self.assertEqual(self.worker.ready(KEY,self.selected),[])
        # A process restart uses the persisted cursor and files.
        restarted=EmbeddedSubtitles(a.__dict__)
        with patch.object(restarted,'transport'):
            self.assertTrue(restarted.once(time.monotonic()+15));self.assertTrue(restarted.once(time.monotonic()+15))
        self.assertEqual(self.row()[:2],('ready',30))
        ready=self.worker.ready(KEY,self.selected);self.assertEqual(len(ready),1)
        self.assertEqual(cues(Path(ready[0]['path']).read_text()),cues(SRT))
        self.assertEqual(ready[0]['source_id'],self.sid)
        self.assertEqual(self.worker.ready(KEY,{**self.selected,'hash':'b'*40}),[])
        with a.F.db() as c:proof=json.loads(c.execute('SELECT proof FROM validation WHERE key=?',(KEY,)).fetchone()[0])
        self.assertEqual(proof,{'version':5,'source_id':self.sid})
    def test_busy_or_protected_title_never_reads_source(self):
        self.seed()
        with patch.object(a,'detail_playback_busy',return_value=True),patch.object(self.worker,'extract') as extract:
            self.assertFalse(self.worker.once(time.monotonic()+15));extract.assert_not_called()
        with a.F.db() as c:c.execute('INSERT INTO verification_holds VALUES (?,?,?)',(KEY,time.time()+3600,'test'))
        with patch.object(self.worker,'extract') as extract:
            self.assertFalse(self.worker.once(time.monotonic()+15));extract.assert_not_called()
        self.assertEqual(self.row()[0],'queued')
    def test_cancellation_keeps_previous_chunks_and_never_invalidates_video(self):
        self.seed();self.worker.once(time.monotonic()+15)
        with patch.object(self.worker,'extract',side_effect=a.ClientCancelled('new playback')),patch.object(a.F,'validation_result') as invalidated:
            self.assertFalse(self.worker.once(time.monotonic()+15));invalidated.assert_not_called()
        self.assertEqual(self.row()[:2],('queued',10));self.assertEqual(self.worker.ready(KEY,self.selected),[])
        with self.worker.db() as c:retry=c.execute('SELECT next_try FROM embedded_subtitles WHERE key=?',(KEY,)).fetchone()[0]
        self.assertGreater(retry,time.time());self.assertLess(retry,time.time()+20)
    def test_new_validation_source_fences_queued_job(self):
        self.seed();a.F.validation_result(KEY,True,proof={'version':5,'source_id':'b'*64})
        with patch.object(self.worker,'extract') as extract:self.assertTrue(self.worker.once(time.monotonic()+15));extract.assert_not_called()
        self.assertEqual(self.row()[0],'stale')
    def test_changed_persisted_transport_cannot_reuse_old_binding(self):
        self.seed()
        with self.worker.db() as c:c.execute('UPDATE embedded_subtitles SET selected=? WHERE key=?',(json.dumps({**self.selected,'hash':'b'*40}),KEY))
        with patch.object(self.worker,'extract') as extract:
            self.assertTrue(self.worker.once(time.monotonic()+15));extract.assert_not_called()
        self.assertEqual(self.row()[0],'stale')
    def test_absent_hebrew_does_not_fabricate_track(self):
        self.seed();info=copy.deepcopy(self.info)
        info['streams']=[s for s in info['streams'] if s.get('codec_type')!='subtitle']
        with patch.object(self.worker,'probe',return_value=info):self.assertTrue(self.worker.once(time.monotonic()+15))
        self.assertEqual(self.row()[0],'none');self.assertEqual(self.worker.ready(KEY,self.selected),[])
    def test_evicted_torrent_is_readded_without_changing_selected_file(self):
        revived=EmbeddedSubtitles(a.__dict__)
        status={'hash':self.selected['hash'],'file_stats':[{'id':1,'path':'fixture.mkv','length':self.selected['length']}]}
        missing=a.urllib.error.HTTPError('private-source',404,'missing',{},None)
        with patch.object(a,'torr',side_effect=[missing,status]) as torrent:
            revived.transport(self.selected)
        self.assertEqual(torrent.call_args_list[1].args[0]['action'],'add')
    def test_incomplete_packet_coverage_cannot_publish_even_if_subtitle_has_cues(self):
        raw='#tb 0: 1/1000\n0,0,1000,40,100,0x00\n0,0,2000,40,100,0x00\n'
        with self.assertRaises(SubtitleCheckpoint):covered(raw,0,10)
        with patch.object(a,'run_preflight',return_value=subprocess.CompletedProcess([],0,raw.encode(),b'')):
            with self.assertRaises(SubtitleCheckpoint):self.worker.extract(KEY,self.selected,self.info,1,0,10)
    def test_cached_embedded_delivery_survives_provider_failure_and_is_source_bound(self):
        self.seed()
        for _ in range(3):self.worker.once(time.monotonic()+15)
        video=copy.deepcopy(self.info)
        # Native video policy allows this synthetic H264 source.
        proof={'version':5,'checked_at':time.time()}
        with patch.object(a,'signature',return_value='fixture'),patch.object(a,'resolve',return_value=self.selected),patch.object(a,'probe',return_value=video),patch.object(a.REL,'verify',return_value=proof),patch.object(a,'external_subtitles',side_effect=TimeoutError),patch.object(a,'EMBED',self.worker):
            result=a.prepared(KEY)
        he=[s for s in result['MediaStreams'] if s['Type']=='Subtitle' and s['Language']=='heb']
        self.assertEqual(len(he),1);self.assertTrue(he[0]['IsExternal']);self.assertEqual(he[0]['Title'],'עברית 1')
        self.assertIn('variant=',he[0]['DeliveryUrl']);self.assertEqual([s['Index'] for s in result['MediaStreams']],list(range(len(result['MediaStreams']))))

if __name__=='__main__':unittest.main()
