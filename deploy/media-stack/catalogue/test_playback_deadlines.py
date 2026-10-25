"""Playback regressions using isolated state and local synthetic HTTP sources."""
import io
import json
import socket
import tempfile
import threading
import time
import unittest
from contextlib import redirect_stdout
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch
from test_app import a

KEY='movie:tt00000000'
SELECTED={'hash':'a'*40,'filename':'fixture.mp4','index':1,'length':100}
INFO={'streams':[{'index':0,'codec_type':'video','codec_name':'h264','pix_fmt':'yuv420p'},
                 {'index':1,'codec_type':'audio','codec_name':'aac','profile':'LC'}],
      'format':{'duration':'60'}}

class SlowSource(BaseHTTPRequestHandler):
    def log_message(self,*args):pass
    def do_GET(self):
        if self.path=='/range':
            self.send_response(206);self.send_header('Content-Length','4');self.send_header('Content-Range','bytes 10-13/100');self.end_headers();self.wfile.write(b'data');return
        if self.path=='/short':
            self.send_response(200);self.send_header('Content-Length','100');self.end_headers();self.wfile.write(b'short');return
        self.send_response(200);self.send_header('Content-Length','100');self.end_headers()
        try:
            if self.path=='/empty':time.sleep(.6);return
            for _ in range(100):self.wfile.write(b'x');self.wfile.flush();time.sleep(.04)
        except (BrokenPipeError,ConnectionResetError):pass

class DeadlineTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server=ThreadingHTTPServer(('127.0.0.1',0),SlowSource)
        cls.thread=threading.Thread(target=cls.server.serve_forever,daemon=True);cls.thread.start()
        cls.url='http://127.0.0.1:'+str(cls.server.server_port)
    @classmethod
    def tearDownClass(cls):cls.server.shutdown();cls.server.server_close();cls.thread.join()

    def test_trickling_provider_obeys_total_budget(self):
        started=time.monotonic()
        with a.request_budget(started+.2),self.assertRaises((TimeoutError,socket.timeout)):
            a.fetch(self.url+'/trickle',raw=True)
        self.assertLess(time.monotonic()-started,.7)

    def test_initial_video_byte_wait_is_bounded(self):
        started=time.monotonic()
        with a.R.urlopen(self.url+'/empty',timeout=.2) as response:
            with self.assertRaises((TimeoutError,socket.timeout)):
                a.read_bounded(response,1,started+.2)
        self.assertLess(time.monotonic()-started,.7)

    def test_bridge_returns_503_before_committing_empty_success(self):
        bridge=ThreadingHTTPServer(('127.0.0.1',0),a.Handler)
        thread=threading.Thread(target=bridge.serve_forever,daemon=True);thread.start()
        try:
            with patch.object(a,'signature',return_value='fixture'),patch.object(a.REL,'lookup',return_value=SELECTED) as resolve,patch.object(a,'upstream',return_value=self.url+'/empty'),patch.object(a,'torr_headers',return_value={}),patch.object(a,'FIRST_DATA_SECONDS',.15),patch.object(a.F,'validation_result') as invalidated:
                started=time.monotonic()
                with self.assertRaises(a.urllib.error.HTTPError) as error:
                    a.R.urlopen('http://127.0.0.1:'+str(bridge.server_port)+'/play/'+KEY+'?sig=fixture&source='+('a'*64),timeout=2)
                self.assertEqual(error.exception.code,503)
                self.assertLess(time.monotonic()-started,.7)
                resolve.assert_called_once()
                invalidated.assert_called_once_with(KEY,False,reason='Stream read failure')
        finally:bridge.shutdown();bridge.server_close();thread.join()

    def test_http_range_is_preserved_by_stream_proxy(self):
        bridge=ThreadingHTTPServer(('127.0.0.1',0),a.Handler)
        thread=threading.Thread(target=bridge.serve_forever,daemon=True);thread.start()
        try:
            with patch.object(a,'signature',return_value='fixture'),patch.object(a.REL,'lookup',return_value=SELECTED),patch.object(a,'upstream',return_value=self.url+'/range'),patch.object(a,'torr_headers',return_value={}):
                request=a.R.Request('http://127.0.0.1:'+str(bridge.server_port)+'/play/'+KEY+'?sig=fixture&source='+('a'*64),headers={'Range':'bytes=10-13'})
                with a.R.urlopen(request,timeout=2) as response:
                    self.assertEqual(response.status,206)
                    self.assertEqual(response.headers['Content-Range'],'bytes 10-13/100')
                    self.assertEqual(response.read(),b'data')
        finally:bridge.shutdown();bridge.server_close();thread.join()

    def test_stream_failure_after_delivery_does_not_switch_sources(self):
        import http.client
        bridge=ThreadingHTTPServer(('127.0.0.1',0),a.Handler)
        thread=threading.Thread(target=bridge.serve_forever,daemon=True);thread.start()
        try:
            with patch.object(a,'signature',return_value='fixture'),patch.object(a.REL,'lookup',return_value=SELECTED) as resolve,patch.object(a,'upstream',return_value=self.url+'/short'),patch.object(a,'torr_headers',return_value={}),patch.object(a.F,'validation_result') as invalidated:
                with a.R.urlopen('http://127.0.0.1:'+str(bridge.server_port)+'/play/'+KEY+'?sig=fixture&source='+('a'*64),timeout=2) as response:
                    with self.assertRaises(http.client.IncompleteRead):response.read()
                resolve.assert_called_once()
                invalidated.assert_called_once_with(KEY,False,reason='Stream read failure')
        finally:bridge.shutdown();bridge.server_close();thread.join()

    def test_locked_preparation_returns_busy_without_invalidating_source(self):
        lock=threading.Lock();lock.acquire()
        started=time.monotonic()
        try:
            with patch.dict(a.LOCKS,{KEY:lock}),patch.object(a,'PREPARATION_SECONDS',.2),patch.object(a.F,'validation_result') as result:
                with self.assertRaises(a.PlaybackBusy):a.prepared(KEY)
                result.assert_not_called()
        finally:lock.release()
        self.assertLess(time.monotonic()-started,.7)

    def test_client_cancel_does_not_invalidate_source(self):
        with patch.object(a.F,'validation_result') as result:
            with self.assertRaises(a.ClientCancelled):a.prepared(KEY,cancel=lambda:True)
            result.assert_not_called()

    def test_disconnect_during_probe_preserves_existing_success(self):
        cancelled=[False]
        def probe(*args,**kwargs):
            cancelled[0]=True
            raise TimeoutError('Probe interrupted')
        with tempfile.TemporaryDirectory() as directory,patch.object(a,'DATA',Path(directory)),patch.object(a,'resolve',return_value=SELECTED),patch.object(a,'probe',side_effect=probe):
            a.F.validation_result(KEY,True,proof={'audio_policy':2})
            with self.assertRaises(a.ClientCancelled):a.prepared(KEY,cancel=lambda:cancelled[0])
            self.assertEqual(a.F.source_state(KEY),'verified')

    def test_busy_stream_does_not_invalidate_or_resolve_other_source(self):
        with patch.dict(a.LIVE,{'active':1,'key':'movie:tt11111111'}),patch.object(a.F,'validation_result') as result,patch.object(a,'resolve') as resolve:
            with self.assertRaises(a.PlaybackBusy):a.prepared(KEY)
            result.assert_not_called();resolve.assert_not_called()

    def test_failed_candidate_falls_back_and_preserves_same_deadline(self):
        other={**SELECTED,'hash':'b'*40}
        deadlines=[]
        def resolve(key,deadline):deadlines.append(deadline);return SELECTED if len(deadlines)==1 else other
        with tempfile.TemporaryDirectory() as directory,patch.object(a,'DATA',Path(directory)),patch.object(a,'resolve',side_effect=resolve),patch.object(a,'probe',side_effect=[RuntimeError('bad source'),INFO]),patch.object(a.REL,'verify',return_value={'version':5}),patch.object(a,'signature',return_value='fixture'),patch.object(a,'stream_candidates',return_value=[{'infoHash':other['hash']}]),patch.object(a,'torr',return_value={}),patch.object(a,'external_subtitles',return_value=[]):
            result=a.prepared(KEY)
            self.assertEqual(result['selected']['hash'],other['hash'])
            self.assertEqual(deadlines[0],deadlines[1])
            self.assertEqual(a.F.source_state(KEY),'verified')

    def test_expired_resolution_keeps_old_proof_but_invalidates_preparation(self):
        with tempfile.TemporaryDirectory() as directory,patch.object(a,'DATA',Path(directory)):
            a.F.validation_result(KEY,True,proof={'audio_policy':2,'fixture':'previous proof'})
            a.remember('prepared:'+KEY,{'audio_policy':1},100)
            def resolve(key,deadline):
                with a.request_budget(time.monotonic()-.1):a.remaining(1)
            with patch.object(a,'resolve',side_effect=resolve):
                with self.assertRaises(a.Checkpoint):a.prepared(KEY)
            self.assertEqual(a.F.source_state(KEY),'verified')
            self.assertEqual(a.cached('prepared:'+KEY),{'audio_policy':1})
            with a.F.db() as connection:proof=connection.execute('SELECT proof FROM validation WHERE key=?',(KEY,)).fetchone()[0]
            self.assertEqual(json.loads(proof)['fixture'],'previous proof')

    def test_diagnostics_never_log_exception_urls_or_tokens(self):
        output=io.StringIO()
        with redirect_stdout(output):
            a.playback_event(KEY,'video_probe',time.monotonic(),'failed',RuntimeError('https://secret.invalid/?api_key=private'))
        text=output.getvalue()
        self.assertIn('RuntimeError',text);self.assertNotIn('secret.invalid',text);self.assertNotIn('private',text)

if __name__=='__main__':unittest.main()
