"""Real FFmpeg evidence, durable audit state and exact source regressions."""
import json
import subprocess
import tempfile
import threading
import time
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch
from test_app import a
from reliability import rank, evidence, Reliability, Checkpoint

KEY='movie:tt00000000'

class ReliabilityTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name)
        self.data=patch.multiple(a,DATA=self.root/'data',LIBRARY=self.root/'library',SECRET=self.root/'secret')
        self.data.start();self.addCleanup(self.data.stop)
        (self.root/'secret').write_text('fixture-secret')
        a.F.db().close();a.REL.db().close()

    def fixture(self,name='fixture.mkv',audio='aac',delayed=False,multiple=False,size='64x64'):
        path=self.root/name
        command=['ffmpeg','-nostdin','-v','error','-threads','1','-f','lavfi','-i',f'testsrc2=s={size}:r=10:d=16',
                 '-f','lavfi','-i','sine=frequency=440:duration=16']
        if delayed:command+=['-filter_complex','[0:v]setpts=PTS+10/TB[v]','-map','[v]','-map','1:a:0']
        else:command+=['-map','0:v:0','-map','1:a:0']
        if multiple:command+=['-map','1:a:0']
        command+=['-c:v','libx264','-preset','ultrafast','-threads','1','-c:a',audio,'-y',str(path)]
        subprocess.run(command,check=True,capture_output=True,timeout=20)
        info=json.loads(subprocess.check_output(['ffprobe','-v','error','-show_streams','-show_format','-of','json',str(path)]))
        selected={'local':str(path),'filename':name,'hash':'a'*40,'index':1,'length':path.stat().st_size}
        return selected,info

    def test_real_lc_samples_decode_beginning_and_rotating_seek(self):
        selected,info=self.fixture()
        proof=a.REL.verify(KEY,selected,info,1,False,time.monotonic()+30)
        self.assertEqual(proof['version'],5);self.assertEqual([s['seconds'] for s in proof['samples']],[6,2])
        self.assertGreater(proof['samples'][0]['video_frames'],50)
        a.F.validation_result(KEY,True,proof=proof)
        second=a.REL.verify(KEY,selected,info,1,False,time.monotonic()+30)
        self.assertNotEqual(proof['samples'][1]['position'],second['samples'][1]['position'])

    def test_background_preparation_is_reused_for_play_with_subtitle_lookup(self):
        selected,info=self.fixture()
        with patch.object(a,'resolve',return_value=selected),patch.object(a,'probe',return_value=info):
            background=a.prepared(KEY,background=True)
        with patch.object(a.F,'local_download',return_value=selected),patch.object(a.REL,'verify',side_effect=AssertionError('Fresh prepared evidence should be reused')),patch.object(a,'probe',side_effect=AssertionError('No duplicate probe')),patch.object(a,'external_subtitles',return_value=[]) as subtitles:
            foreground=a.prepared(KEY)
        subtitles.assert_called_once()
        self.assertEqual(background['checked_at'],foreground['checked_at'])
        self.assertEqual(background['PlaybackUrl'],foreground['PlaybackUrl'])
        # A scheduled audit must ignore that foreground result and decode again.
        with patch.object(a,'resolve',return_value=selected),patch.object(a,'probe',return_value=info),patch.object(a.REL,'verify',side_effect=RuntimeError('Audit must decode')) as verifier:
            with self.assertRaises(RuntimeError):a.prepared(KEY,background=True)
            self.assertGreater(verifier.call_count,0)

    def test_real_audio_conversion_is_lc_stereo_and_video_copy(self):
        selected,info=self.fixture(audio='ac3')
        proof=a.REL.verify(KEY,selected,info,1,True,time.monotonic()+30)
        self.assertTrue(proof['audio_conversion']);self.assertTrue(proof['video_copy'])
        self.assertGreater(proof['samples'][0]['audio_frames'],100)
        self.assertFalse(list(a.DATA.glob('verify-*')))

    def test_manual_request_cannot_reuse_an_older_partial_beginning_sample(self):
        selected,info=self.fixture();a.F.validation_result(KEY,True,proof={'version':5,'audit_sequence':0})
        requested=time.time()
        with a.F.db() as c:c.execute('INSERT INTO manual_rechecks VALUES (?,?,?,?)',(KEY,requested,'fixture','pending'))
        a.REL.checkpoint(KEY,sample_checkpoint={'identity':[a.REL.sid(selected),1,1,0,False],
            'checked_at':requested-1,'evidence':{'video_frames':999,'audio_frames':999}})
        with patch.object(a.REL,'sample',return_value={'video_frames':60,'audio_frames':100}) as sample:
            proof=a.REL.verify(KEY,selected,info,1,False,time.monotonic()+10)
        self.assertEqual(sample.call_count,2);self.assertEqual(sample.call_args_list[0].args[5],0)
        self.assertEqual(proof['samples'][0]['video_frames'],60)

    def test_real_exit_zero_delayed_video_cannot_pass_full_preparation(self):
        selected,info=self.fixture(delayed=True)
        with patch.object(a,'resolve',return_value=selected),patch.object(a,'probe',return_value=info):
            with self.assertRaises(RuntimeError):a.prepared(KEY,background=True)
        self.assertEqual(a.F.source_state(KEY),'unavailable')
        with a.F.db() as c:self.assertIsNone(c.execute('SELECT proof FROM validation WHERE key=?',(KEY,)).fetchone()[0])

    def test_only_selected_audio_is_mapped_and_later_failure_rejects(self):
        selected,info=self.fixture(multiple=True)
        original=a.REL.run;commands=[]
        def run(command,*args,**kwargs):commands.append(command);return original(command,*args,**kwargs)
        with patch.object(a.REL,'run',side_effect=run):
            a.REL.verify(KEY,selected,info,2,False,time.monotonic()+30)
        self.assertTrue(all('0:2' in c and '0:1' not in c for c in commands if c[0]=='ffmpeg'))
        original_sample=a.REL.sample
        def sample(*args):
            if args[5]>0:raise RuntimeError('Later sample corrupted')
            return original_sample(*args)
        with patch.object(a.REL,'sample',side_effect=sample),self.assertRaises(RuntimeError):
            a.REL.verify(KEY,selected,info,2,False,time.monotonic()+30)

    def test_evidence_rejects_zero_frames_and_sparse_discontinuous_frames(self):
        for output in ('','#tb 0: 1/10\n#media_type 0: video\n0, 0, 0, 1, 1, 0x0\n0, 100, 100, 1, 1, 0x0'):
            with self.assertRaises(RuntimeError):evidence(output,6,False)

    def test_exact_binding_survives_different_title_resolution_and_reboot(self):
        one={'hash':'a'*40,'index':1,'filename':'one.mkv','length':123}
        two={'hash':'b'*40,'index':2,'filename':'two.mkv','length':456}
        sid=a.REL.bind(KEY,one);a.remember('resolved:'+KEY,two,900)
        self.assertEqual(Reliability(a.__dict__).lookup(KEY,sid),one)
        url=a.REL.source_url('play',KEY,one)
        self.assertIn('source='+sid,url);self.assertNotEqual(a.signature(KEY),a.U.parse_qs(a.U.urlsplit(url).query)['sig'][0])
        with self.assertRaises(ValueError):a.REL.lookup('movie:tt00000001',sid)

    def test_saved_winner_then_verified_backup_before_new_peers(self):
        one={'hash':'a'*40,'index':1,'filename':'one.mkv','length':123}
        two={'hash':'b'*40,'index':2,'filename':'two.mkv','length':456}
        a.REL.success(KEY,two);a.REL.success(KEY,one)
        self.assertEqual(a.REL.choices(KEY)[0],one)
        a.REL.exclude(KEY,one);self.assertEqual(a.REL.choices(KEY),[two])
        with patch.object(a.F,'local_download',return_value=None),patch.object(a,'torr',return_value={'file_stats':[{'id':2,'path':'two.mkv','length':456}]}),patch.object(a,'stream_candidates',side_effect=AssertionError('Discovery must not run')):
            self.assertEqual(a.resolve(KEY)['hash'],two['hash'])

    def test_rank_relevance_counts_quality_and_unknown_counts(self):
        low={'name':'1080p AAC','seeders':1};high={'name':'720p','peers':5000}
        self.assertGreater(rank(high),rank(low))
        self.assertGreater(rank({'seeders':2}),rank({'peers':2}))
        self.assertGreater(rank({'peers':0}),rank({}))
        with a.db() as c:c.execute('INSERT INTO items VALUES (?,?,?,?,?)',(KEY,'movie','tt00000000',json.dumps({'name':'Test Movie','releaseInfo':'2026'}),'[]'))
        self.assertTrue(a.candidate_matches({'title':'Test.Movie.2026.1080p'},KEY))
        self.assertFalse(a.candidate_matches({'title':'Other.Movie.2026.1080p','peers':99999},KEY))
        self.assertFalse(a.candidate_matches({'title':'Test.Movie.2025.1080p'},KEY))
        self.assertFalse(a.candidate_matches({'title':'Test.Movie.2026.trailer.1080p'},KEY))

    def test_buffer_progress_ignores_other_files_and_repeated_pieces(self):
        selected={'hash':'a'*40};seen={}
        cache={'Hash':'a'*40,'Readers':[{'Reader':5,'Start':4,'End':7}], 'Pieces':{'1':{'Size':9999},'5':{'Size':100},'6':{'Size':50}}}
        self.assertEqual(a.REL.buffer_progress(cache,selected,seen)[0],150)
        cache['Pieces']['1']['Size']=999999
        self.assertEqual(a.REL.buffer_progress(cache,selected,seen)[0],150)
        cache['Pieces']['5']['Size']=110
        self.assertEqual(a.REL.buffer_progress(cache,selected,seen)[0],160)
        cache['Readers'].append({'Reader':1,'End':2})
        self.assertEqual(a.REL.buffer_progress(cache,selected,seen),(0,0))

    def test_checkpoint_is_not_negative_and_resume_bypasses_old_conversion_cache(self):
        a.F.validation_result(KEY,True,proof={'version':4})
        selected,info=self.fixture()
        with patch.object(a,'resolve',return_value=selected),patch.object(a,'probe',side_effect=Checkpoint):
            with self.assertRaises(Checkpoint):a.prepared(KEY)
        self.assertEqual(a.F.source_state(KEY),'verified')
        with a.REL.db() as c:self.assertEqual(json.loads(c.execute('SELECT value FROM preparation_work WHERE key=?',(KEY,)).fetchone()[0])['state'],'checkpoint')
        # A fresh audit cannot accept legacy seven-day conversion cache entries.
        a.remember('audio-convert-v2:'+selected['hash']+':1:'+str(selected['length']),True,99999)
        with patch.object(a.REL,'sample',side_effect=RuntimeError('Fresh decode failed')),self.assertRaises(RuntimeError):
            a.REL.verify(KEY,selected,info,1,False,time.monotonic()+10)

    def test_real_corruption_in_selected_audio_and_later_video_fails(self):
        selected,info=self.fixture(multiple=True)
        original=Path(selected['local']).read_bytes()
        packets=json.loads(subprocess.check_output(['ffprobe','-v','error','-show_packets','-of','json',selected['local']]))['packets']
        def corrupt(stream,after):
            data=bytearray(original);changed=0
            for packet in packets:
                if packet['stream_index']!=stream or float(packet.get('pts_time',0))<after:continue
                start=int(packet['pos']);size=int(packet['size'])
                if size>100:
                    lo=start+max(32,size//4);hi=min(start+size-8,lo+size//2)
                    data[lo:hi]=b'\xff'*(hi-lo);changed+=1
            self.assertGreater(changed,0);Path(selected['local']).write_bytes(data)
        corrupt(2,0)
        with self.assertRaises(RuntimeError):a.REL.verify(KEY,selected,info,2,False,time.monotonic()+30)
        # Corrupting an unselected track does not make us silently map it.
        a.REL.sample(KEY,selected,info,1,False,0,6,time.monotonic()+30)
        corrupt(0,8)
        a.REL.sample(KEY,selected,info,1,False,0,6,time.monotonic()+30)
        with self.assertRaises(RuntimeError):a.REL.sample(KEY,selected,info,1,False,8,2,time.monotonic()+30)

    def test_hd_conversion_seek_stays_within_small_container_memory(self):
        # The later AAC sample starts after video preroll. Full-size wrapped
        # frame packets queued behind that audio used to exhaust 256 MiB.
        selected,info=self.fixture(audio='ac3',size='1920x1080')
        proof=a.REL.verify(KEY,selected,info,1,True,time.monotonic()+60)
        self.assertTrue(proof['audio_conversion'])
        self.assertEqual(next(s['width'] for s in info['streams'] if s['codec_type']=='video'),1920)
        self.assertGreater(proof['samples'][1]['video_frames'],10)

    def test_resource_termination_checkpoints_without_bad_source_proof(self):
        import sys
        from reliability import ResourceCheckpoint
        selected={'hash':'a'*40,'index':1,'filename':'fixture.mkv','length':123}
        command=[sys.executable,'-c','import os,signal;os.kill(os.getpid(),signal.SIGKILL)','-v','error']
        with patch.object(a.REL,'cache_state',return_value={}):
            with self.assertRaises(ResourceCheckpoint):a.REL.run(command,KEY,'sample decode',selected,'f'*32,time.monotonic()+2)
        self.assertFalse(a.REL.excluded(KEY,selected))

    def test_progress_clock_keeps_useful_slow_source_and_rejects_stall(self):
        import sys
        selected={'hash':'a'*40,'index':1,'filename':'fixture.mkv','length':123}
        command=[sys.executable,'-c','import time;time.sleep(.65)','-v','error']
        start=time.monotonic()
        def progress(name):return {'bytes':int((time.monotonic()-start)*1000)}
        with patch('reliability.POLL_SECONDS',.04),patch('reliability.STALL_SECONDS',.15),patch.object(a.REL,'cache_state',return_value={}),patch.object(a,'cached',side_effect=progress):
            result=a.REL.run(command,KEY,'slow download',selected,'f'*32,time.monotonic()+2)
            self.assertEqual(result.returncode,0)
        with patch('reliability.POLL_SECONDS',.04),patch('reliability.STALL_SECONDS',.15),patch.object(a.REL,'cache_state',return_value={'Torrent':{'active_peers':999,'loaded_size':999999}}),patch.object(a,'cached',return_value={}):
            with self.assertRaises(RuntimeError):a.REL.run(command,KEY,'stalled download',selected,'f'*32,time.monotonic()+2)
        with patch('reliability.POLL_SECONDS',.04),patch.object(a.REL,'cache_state',return_value={}),patch.object(a,'cached',side_effect=progress):
            with self.assertRaises(Checkpoint):a.REL.run(command,KEY,'slice ended',selected,'f'*32,time.monotonic()+.1)

    def test_sample_proxy_cancellation_closes_only_its_blocked_upstream(self):
        import select,socket
        from http.server import BaseHTTPRequestHandler
        entered=threading.Event();closed=threading.Event()
        class Waiting(BaseHTTPRequestHandler):
            def log_message(self,*args):pass
            def do_GET(self):
                entered.set()
                end=time.monotonic()+3
                while time.monotonic()<end:
                    if select.select([self.connection],[],[],.05)[0] and self.connection.recv(1,socket.MSG_PEEK)==b'':
                        closed.set();return
        upstream=ThreadingHTTPServer(('127.0.0.1',0),Waiting)
        bridge=ThreadingHTTPServer(('127.0.0.1',0),a.Handler)
        upstream.daemon_threads=bridge.daemon_threads=True
        for server in (upstream,bridge):threading.Thread(target=server.serve_forever,daemon=True).start()
        selected={'hash':'a'*40,'index':1,'filename':'fixture.mkv','length':123}
        try:
            with patch.object(a,'BASE','http://127.0.0.1:'+str(bridge.server_port)),patch.object(a,'upstream',return_value='http://127.0.0.1:'+str(upstream.server_port)+'/wait'),patch.object(a,'torr_headers',return_value={}):
                url=a.REL.source_url('raw',KEY,selected,'f'*32)
                client=socket.create_connection(('127.0.0.1',bridge.server_port),timeout=2)
                client.sendall(('GET '+a.U.urlsplit(url).path+'?'+a.U.urlsplit(url).query+' HTTP/1.1\r\nHost: fixture\r\nConnection: close\r\n\r\n').encode())
                self.assertTrue(entered.wait(1));client.close()
                self.assertTrue(closed.wait(1.5),'Cancelled probe left its torrent reader alive')
        finally:
            for server in (bridge,upstream):server.shutdown();server.server_close()

    def test_worker_session_check_inside_cancellation_does_not_recurse(self):
        with patch.object(a.F,'seed_validation'),patch.object(a.REL,'schedule'),patch.object(a.F,'worker_audit',return_value=(KEY,)),patch.object(a.F,'jf',return_value=[]) as sessions,patch.object(a,'prepared') as prepare:
            a.F.enqueue(KEY)
            def check(*args,**kwargs):
                # Mimic fetch checking remaining() inside the native session request.
                calls=[0]
                def api(*args,**kwargs):calls[0]+=1;a.remaining(1);return []
                with patch.object(a.F,'jf',side_effect=api):
                    with a.request_budget(time.monotonic()+2,kwargs['cancel']):a.remaining(1)
                self.assertEqual(calls[0],1)
            prepare.side_effect=check
            with patch('features.time.sleep'):a.F.warm_availability(1)

    def test_initial_audit_preserves_visible_rows_and_prioritizes_old_proof(self):
        a.F.validation_result(KEY,True,proof={'version':4})
        a.F.validation_result('movie:tt00000001',True,proof={'version':5})
        a.F.validation_result('movie:tt00000002',True,proof={'version':3})
        a.F.validation_result('movie:tt00000002',False)
        a.REL.schedule();a.REL.schedule()
        self.assertEqual(a.F.source_state(KEY),'verified')
        self.assertEqual(a.REL.next_audit()[0],KEY)
        with a.REL.db() as c:self.assertEqual(c.execute('SELECT COUNT(*) FROM verification_audit').fetchone()[0],3)
        a.F.validation_result(KEY,False,reason='Fresh decode failed')
        self.assertEqual(a.F.source_state(KEY),'unavailable')

if __name__=='__main__':unittest.main()
