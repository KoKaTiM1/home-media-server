"""Speculative playback must help Play without harming viewing or readiness."""
import concurrent.futures
import threading
import time
import unittest
from unittest.mock import patch
import test_features as fixtures
a=fixtures.a
KEY='movie:tt1234567'
SELECTED={'hash':'a'*40,'filename':'fixture.mp4','index':1,'length':100}
INFO={'streams':[{'index':0,'codec_type':'video','codec_name':'h264','pix_fmt':'yuv420p'},
                 {'index':1,'codec_type':'audio','codec_name':'aac','profile':'LC'}],
      'format':{'duration':'60'}}

class DetailTests(unittest.TestCase):
    setUp=fixtures.FeatureTests.setUp

    def setup_warm(self):
        self.limit=threading.Semaphore(1)
        self.patches=patch.multiple(a,DETAIL_LIMIT=self.limit,DETAIL_RECENT={})
        self.patches.start();self.addCleanup(self.patches.stop)

    def wait_warm(self):
        self.assertTrue(self.limit.acquire(timeout=3),'Details worker did not finish')
        self.limit.release()

    def test_no_preparation_during_active_or_paused_playback(self):
        self.setup_warm()
        with patch.object(a,'jf',return_value=[{'NowPlayingItem':{},'PlayState':{'IsPaused':True}}]),patch.object(a,'prepared') as prepare:
            # A real NowPlayingItem is nonempty.
            with patch.object(a,'jf',return_value=[{'NowPlayingItem':{'Id':'fixture'},'PlayState':{'IsPaused':True}}]):
                self.assertTrue(a.warm_details(KEY));self.wait_warm()
            prepare.assert_not_called()
        with patch.object(a,'jf',side_effect=TimeoutError),patch.object(a,'prepared') as prepare:
            self.assertTrue(a.warm_details('movie:tt2345678'));self.wait_warm();prepare.assert_not_called()

    def test_one_slot_no_queue_and_repeat_cooldown(self):
        self.setup_warm();entered=threading.Event();release=threading.Event()
        def prepare(*args,**kwargs):entered.set();release.wait(2)
        with patch.object(a,'detail_playback_busy',return_value=False),patch.object(a,'prepared',side_effect=prepare) as prepared:
            self.assertTrue(a.warm_details(KEY));self.assertTrue(entered.wait(1))
            try:
                self.assertFalse(a.warm_details('movie:tt2345678'))
                self.assertFalse(a.warm_details(KEY))
            finally:release.set();self.wait_warm()
            self.assertFalse(a.warm_details(KEY));self.assertEqual(prepared.call_count,1)

    def test_warm_result_reused_by_play_without_new_probe_or_subtitle_lookup(self):
        self.setup_warm();a.F.validation_result(KEY,True,proof={'audio_policy':2})
        with patch.object(a,'detail_playback_busy',return_value=False),patch.object(a,'resolve',return_value=SELECTED) as resolve,patch.object(a,'probe',return_value=INFO) as probe,patch.object(a,'external_subtitles',return_value=[]) as subs:
            self.assertTrue(a.warm_details(KEY));self.wait_warm()
            result=a.prepared(KEY)
            self.assertEqual(result['selected'],SELECTED)
            self.assertEqual(resolve.call_count,1);self.assertEqual(probe.call_count,1);self.assertEqual(subs.call_count,1)

    def test_failed_warm_does_not_hide_title_or_drop_existing_torrent(self):
        self.setup_warm();a.F.validation_result(KEY,True,proof={'audio_policy':2,'fixture':'old proof'})
        with patch.object(a,'detail_playback_busy',return_value=False),patch.object(a,'resolve',return_value=SELECTED),patch.object(a,'probe',side_effect=TimeoutError),patch.object(a,'torr') as torr:
            self.assertTrue(a.warm_details(KEY));self.wait_warm()
            self.assertEqual(a.F.source_state(KEY),'verified');torr.assert_not_called()
            self.assertIsNone(a.cached('source-status:'+KEY))

    def test_failed_resolution_does_not_change_readiness(self):
        self.setup_warm();a.F.validation_result(KEY,True,proof={'audio_policy':2})
        with patch.object(a,'detail_playback_busy',return_value=False),patch.object(a,'resolve',side_effect=TimeoutError):
            self.assertTrue(a.warm_details(KEY));self.wait_warm()
            self.assertEqual(a.F.source_state(KEY),'verified')

    def test_playback_start_cancels_preparation_without_negative_proof(self):
        self.setup_warm();a.F.validation_result(KEY,True,proof={'audio_policy':2})
        checks=[False,False,True]
        def busy():return checks.pop(0) if checks else True
        def probe(*args,**kwargs):time.sleep(1.05);a.remaining(1)
        with patch.object(a,'detail_playback_busy',side_effect=busy),patch.object(a,'resolve',return_value=SELECTED),patch.object(a,'probe',side_effect=probe):
            self.assertTrue(a.warm_details(KEY));self.wait_warm()
            self.assertEqual(a.F.source_state(KEY),'verified')

    def test_speculative_call_never_waits_for_foreground_lock(self):
        lock=threading.Lock();lock.acquire()
        try:
            with patch.dict(a.LOCKS,{KEY:lock}),patch.object(a.BUDGET,'speculative',True,create=True),patch.object(a,'resolve') as resolve:
                with self.assertRaises(a.PlaybackBusy):a.prepared(KEY)
                resolve.assert_not_called()
        finally:lock.release()

if __name__=='__main__':unittest.main()

class DetailRouteTests(unittest.TestCase):
    setUp=fixtures.FeatureTests.setUp
    def test_only_playable_details_get_starts_preparation(self):
        from http.server import ThreadingHTTPServer
        import urllib.request,json
        server=ThreadingHTTPServer(('127.0.0.1',0),a.Handler)
        thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
        url=f'http://127.0.0.1:{server.server_port}/stream/movie/tt1234567.json'
        try:
            with patch.object(a,'warm_details') as warm,patch.object(a,'signature',return_value='fixture'):
                with urllib.request.urlopen(url,timeout=2) as response:
                    self.assertEqual(json.load(response),{'streams':[]})
                warm.assert_not_called()
                a.F.validation_result(KEY,True,proof={'audio_policy':2})
                with urllib.request.urlopen(urllib.request.Request(url,method='HEAD'),timeout=2) as response:self.assertEqual(response.status,200)
                warm.assert_not_called()
                with urllib.request.urlopen(url,timeout=2) as response:self.assertTrue(json.load(response)['streams'])
                warm.assert_called_once_with(KEY)
        finally:server.shutdown();server.server_close();thread.join()

class SessionCancellationTests(unittest.TestCase):
    setUp=fixtures.FeatureTests.setUp
    def test_real_session_request_inside_cancel_callback_has_no_recursion(self):
        from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
        import json
        responses=[[],[{'NowPlayingItem':{'Id':'fixture'},'PlayState':{'IsPaused':True}}]]
        calls=[]
        class Sessions(BaseHTTPRequestHandler):
            def log_message(self,*args):pass
            def do_GET(self):
                calls.append(self.path)
                self.send_response(200);self.end_headers();self.wfile.write(json.dumps(responses[len(calls)-1]).encode())
        server=ThreadingHTTPServer(('127.0.0.1',0),Sessions)
        thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
        secret=self.root/'test-key';secret.write_text('fixture-key')
        try:
            with patch.multiple(a,JF=f'http://127.0.0.1:{server.server_port}',JFKEY=secret),a.request_budget(time.monotonic()+3,a.detail_playback_busy):
                self.assertGreater(a.remaining(1),0,'An idle session must allow speculative preparation')
                self.assertEqual(calls,['/Sessions'])
                self.assertIs(a.BUDGET.cancel,a.detail_playback_busy,'Restore the outer cancellation callback')
                with self.assertRaises(a.ClientCancelled):a.remaining(1)
                self.assertEqual(calls,['/Sessions','/Sessions'])
        finally:server.shutdown();server.server_close();thread.join()
