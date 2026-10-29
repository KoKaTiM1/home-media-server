"""Whole-title capability/fallback fences and real local HTTP/FFmpeg evidence."""
import copy
from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
import json
import os
from pathlib import Path
import re
import struct
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
from test_app import a
from verification_jobs import DecodeJobs,WorkerWait
from verification_title import validate_evidence,validate_task,execute_title
from reliability import Checkpoint

KEY='movie:tt0000001';HASH='a'*40
class TitleTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup);self.root=Path(self.tmp.name)
        p=patch.multiple(a,DATA=self.root/'server',SECRET=self.root/'secret');p.start();self.addCleanup(p.stop)
        (self.root/'secret').write_text('fixture-only')
        a.F.db().close();a.REL.db().close()
        self.meta={'name':'Fixture','year':2026,'id':'tt0000001','type':'movie'}
        with a.db() as c:c.execute('INSERT INTO items VALUES (?,?,?,?,?)',(KEY,'movie','tt0000001',json.dumps(self.meta),'[]'))
        self.payload={'schema':1,'key':KEY,'meta':self.meta,'candidates':[{'infoHash':HASH,'title':'Fixture 2026 1080p','behaviorHints':{'filename':'Fixture.2026.mp4'}}], 'aliases':[],'prior':{'state':'pending'}}
        self.q=DecodeJobs(self.root/'queue.sqlite');self.q.initialize();self.q.set('enabled',True);self.q.set('local_torrent_enabled',True);self.q.set('local_torrent_permitted',True);self.q.set('max_windows_jobs',4)
        self.q.claim('pc-new',['local-torrent-v1']);self.q.backend_health('pc-new',True)
    def test_old_client_cannot_take_whole_title_and_stale_lease_is_fenced(self):
        job=self.q.submit(KEY,'b'*64,'verify_title',self.payload,'',60)
        self.assertIsNone(self.q.claim('old-decoder'))
        task=self.q.claim('pc-new',['local-torrent-v1']);self.assertEqual(task['job_id'],job)
        self.assertEqual(self.q.local_workers(),1)
        self.q.finish(job)
        self.assertEqual(self.q.receive('pc-new',task,{'status':'checkpoint','reason':'transport','result_id':'c'*32}),'stale')
    def test_local_failure_uses_server_once_without_false_unavailable(self):
        def poll(job):return 'result',{'status':'checkpoint','reason':'transport'}
        calls=[]
        a.BUDGET.remote_verification=True;a.BUDGET.worker_deadline=time.monotonic()+60
        with patch('verification_remote.jobs',return_value=self.q),patch('verification_title.payload',return_value=self.payload),patch.object(self.q,'poll',side_effect=poll),patch.object(a,'prepared',side_effect=lambda *args,**kw:calls.append(a.BUDGET.remote_verification)):
            self.assertTrue(execute_title(a.__dict__,KEY))
        self.assertEqual(calls,[False]);self.assertTrue(a.BUDGET.remote_verification)
        with a.F.db() as c:self.assertIsNone(c.execute('SELECT state FROM validation WHERE key=?',(KEY,)).fetchone())
    def test_local_failure_defers_fallback_while_debian_gate_is_paused(self):
        self.q.set('enabled',False)
        a.BUDGET.remote_verification=True;a.BUDGET.worker_deadline=time.monotonic()+60
        with patch('verification_remote.jobs',return_value=self.q),patch('verification_title.payload',return_value=self.payload),patch.object(self.q,'poll',return_value=('result',{'status':'checkpoint','reason':'transport'})),patch.object(a,'prepared') as prepared:
            with self.assertRaises(Checkpoint):execute_title(a.__dict__,KEY)
        prepared.assert_not_called()
        self.assertEqual(self.q.status()['totals'].get('windows_local_fallback_deferred'),1)
    def test_unknown_publisher_and_hash_rejected(self):
        for c in ({'infoHash':'invalid'}, {'infoHash':HASH,'torrentUrl':'http://private.invalid/secret'}):
            p=copy.deepcopy(self.payload);p['candidates']=[c]
            with self.assertRaises(ValueError):validate_task(p)
    def test_pilot_reserves_exactly_twenty_dispatches(self):
        self.q.set('prefetch_per_lane',8)
        self.q.set('local_torrent_pilot_remaining',20)
        jobs=[]
        for n in range(20):
            job=self.q.submit(KEY,format(n,'064x'),'verify_title',self.payload,'',60)
            jobs.append(job)
            self.q.finish(job)
        with self.assertRaises(WorkerWait):
            self.q.submit(KEY,'f'*64,'verify_title',self.payload,'',60)
        self.assertEqual(self.q.setting_read('local_torrent_pilot_remaining'),0)
    def test_queue_busy_keeps_debian_path_available(self):
        self.q.set('local_torrent_pilot_remaining',0)
        a.BUDGET.remote_verification=True;a.BUDGET.worker_deadline=time.monotonic()+60
        with patch('verification_remote.jobs',return_value=self.q),patch('verification_title.payload',return_value=self.payload):
            self.assertFalse(execute_title(a.__dict__,KEY))
        self.assertTrue(a.BUDGET.remote_verification)
    def test_local_title_can_run_while_server_stream_gate_is_paused(self):
        self.q.set('enabled',False)
        import verification_remote
        with patch('verification_remote.jobs',return_value=self.q):
            self.assertTrue(verification_remote.ready(a.__dict__))
        job=self.q.submit(KEY,'e'*64,'verify_title',self.payload,'',60)
        self.assertIsNone(self.q.claim('old-decoder'))
        task=self.q.claim('pc-new',['local-torrent-v1'])
        self.assertEqual(task['job_id'],job)
    def test_real_local_retrieval_conversion_speed_and_import_rejection(self):
        fixture=self.root/'media.mp4'
        subprocess.run(['ffmpeg','-nostdin','-v','error','-f','lavfi','-i','testsrc2=s=64x64:r=10:d=60','-f','lavfi','-i','sine=duration=60','-c:v','libx264','-preset','ultrafast','-threads','1','-c:a','ac3','-movflags','+faststart','-y',str(fixture)],check=True,capture_output=True,timeout=30)
        with fixture.open('ab') as f:f.write(struct.pack('>I4s',32*1024**2,b'free'));f.truncate(f.tell()+32*1024**2-8)
        length=fixture.stat().st_size
        class Handler(BaseHTTPRequestHandler):
            def log_message(self,*args):pass
            def do_POST(self):
                self.rfile.read(int(self.headers.get('Content-Length','0')))
                body=({'hash':HASH,'file_stats':[{'id':1,'path':'Fixture.2026.mp4','length':length}]} if self.path=='/torrents' else {'Hash':HASH,'Filled':0,'Readers':[],'Pieces':{}})
                data=json.dumps(body).encode();self.send_response(200);self.send_header('Content-Length',str(len(data)));self.end_headers();self.wfile.write(data)
            def do_GET(self):
                match=re.fullmatch(r'bytes=(\d+)-(\d*)',self.headers.get('Range',''))
                start=int(match[1]) if match else 0;end=min(length-1,int(match[2]) if match and match[2] else length-1)
                self.send_response(206 if match else 200);self.send_header('Accept-Ranges','bytes');self.send_header('Content-Length',str(end-start+1))
                if match:self.send_header('Content-Range',f'bytes {start}-{end}/{length}')
                self.end_headers()
                try:
                    with fixture.open('rb') as f:
                        f.seek(start);left=end-start+1
                        while left:
                            data=f.read(min(left,65536))
                            if not data:break
                            self.wfile.write(data);left-=len(data)
                except (BrokenPipeError,ConnectionResetError):pass
        server=ThreadingHTTPServer(('127.0.0.1',0),Handler);thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start();self.addCleanup(server.server_close);self.addCleanup(server.shutdown)
        payload=self.root/'payload.json';payload.write_text(json.dumps(self.payload));result=self.root/'result.json';state=self.root/'local'
        child=Path(__file__).parents[1]/'verification-worker/local_verifier.py';started=time.time()
        env={**os.environ,'PYTHONPATH':str(Path(__file__).parent),'LOCAL_TORRSERVER_URL':f'http://127.0.0.1:{server.server_port}','LOCAL_TORRENT_STATE':str(self.root/'locks')}
        subprocess.run([sys.executable,str(child),'--payload',str(payload),'--state',str(state),'--output',str(result)],env=env,check=True,capture_output=True,timeout=60)
        value=json.loads(result.read_text());self.assertEqual(value['status'],'ok',value)
        data=value['data'];selected,info,proof,audio,conversion=validate_evidence(a.__dict__,KEY,self.payload,data,started)
        self.assertTrue(conversion);self.assertEqual(len(proof['speed']['windows']),2);self.assertEqual(len(proof['samples']),2)
        self.assertGreater(proof['samples'][0]['video_frames'],1)
        self.assertEqual(proof['speed']['measurement_worker'],'windows')
        for corrupt in ('hash','speed','samples','reuse'):
            bad=copy.deepcopy(data)
            if corrupt=='hash':bad['selected']['hash']='d'*40
            elif corrupt=='speed':bad['proof']['speed']['windows'][0]['bytes_per_second']*=10
            elif corrupt=='samples':bad['raw_samples'][0]['data']['crc']=''
            else:bad['proof']['decode_reused']=True
            with self.assertRaises((ValueError,RuntimeError)):
                validate_evidence(a.__dict__,KEY,self.payload,bad,started)
        with a.validation_attempt(True):a.finish_preparation(KEY,True,time.monotonic()+10,selected,info,proof,audio,conversion)
        with a.F.db() as c:
            row=c.execute('SELECT state,proof FROM validation WHERE key=?',(KEY,)).fetchone()
            self.assertEqual(row[0],'verified');self.assertEqual(json.loads(row[1])['source_backend'],'windows-torrent')

if __name__=='__main__':unittest.main()
