"""Whole-title capability/fallback fences and real local HTTP/FFmpeg evidence."""
import copy
import sqlite3
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
from verification_title import validate_evidence,validate_task,execute_title,controller_fault
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
    def test_remaining_execution_budget_accepts_fractional_elapsed_time(self):
        for seconds in (1,59,239,240,499,500):
            job=self.q.submit(KEY,'b'*64,'verify_title',self.payload,'',seconds,execution_seconds=seconds)
            task=self.q.claim('pc-new',['local-torrent-v1'])
            self.assertEqual(task['job_id'],job)
            self.q.finish(job)

    def test_queue_wait_does_not_spend_worker_execution_budget(self):
        class Clock:
            now=0
            def monotonic(self):return self.now
            def time(self):return 10000+self.now
            def sleep(self,_):pass
        clock=Clock();steps=iter(((140,'pending'),(200,'leased'),(260,'leased'),(350,'result')))
        def poll(job):
            clock.now,state=next(steps)
            return state,({'status':'checkpoint','reason':'transport'} if state=='result' else None)
        def remaining(_):
            if a.BUDGET.deadline is not None and clock.now>=a.BUDGET.deadline:raise TimeoutError('fixture budget')
            return 1
        a.BUDGET.first_sweep_attempt=True;self.addCleanup(delattr,a.BUDGET,'first_sweep_attempt')
        a.BUDGET.worker_deadline=240;a.BUDGET.remote_verification=True
        with patch('verification_title.time',clock),patch('verification_remote.jobs',return_value=self.q),patch('verification_title.payload',return_value=self.payload),patch.object(self.q,'poll',side_effect=poll),patch.object(self.q,'job_clock',return_value={'created':10000,'claimed_at':10200,'deadline':10440,'execution_seconds':240}),patch.object(a,'remaining',side_effect=remaining),patch.object(a,'prepared') as prepared:
            self.assertTrue(execute_title(a.__dict__,KEY))
        prepared.assert_called_once();self.assertEqual(clock.now,350)
        with self.q.db() as c:
            row=c.execute('SELECT execution_seconds,deadline-created FROM decode_jobs').fetchone()
            self.assertEqual(tuple(row),(240,240))
    def test_queue_wait_has_its_own_bounded_timeout(self):
        class Clock:
            now=0
            def monotonic(self):return self.now
            def time(self):return 10000+self.now
            def sleep(self,_):pass
        clock=Clock()
        def poll(job):clock.now=241;return 'pending',None
        def remaining(_):
            if a.BUDGET.deadline is not None and clock.now>=a.BUDGET.deadline:raise TimeoutError('fixture queue budget')
            return 1
        a.BUDGET.first_sweep_attempt=True;self.addCleanup(delattr,a.BUDGET,'first_sweep_attempt')
        a.BUDGET.worker_deadline=240
        with patch('verification_title.time',clock),patch('verification_remote.jobs',return_value=self.q),patch('verification_title.payload',return_value=self.payload),patch.object(self.q,'poll',side_effect=poll),patch.object(a,'remaining',side_effect=remaining):
            with self.assertRaises(TimeoutError):execute_title(a.__dict__,KEY)
        with self.q.db() as c:self.assertEqual(c.execute('SELECT cancel_code FROM decode_jobs').fetchone()[0],'queue_timeout')
    def test_controller_cancellation_preserves_actual_reason(self):
        for reason in ('title_claim_lost','worker_unavailable','navigation_interrupted','playback_interrupted','session_unavailable'):
            a.BUDGET.remote_cancel_code=reason
            a.BUDGET.worker_deadline=time.monotonic()+240
            with patch('verification_remote.jobs',return_value=self.q),patch('verification_title.payload',return_value=self.payload),patch.object(a,'remaining',side_effect=a.ClientCancelled('fixture cancellation')):
                with self.assertRaises(a.ClientCancelled):execute_title(a.__dict__,KEY)
            with self.q.db() as c:self.assertEqual(c.execute('SELECT cancel_code FROM decode_jobs ORDER BY created DESC LIMIT 1').fetchone()[0],reason)
        a.BUDGET.remote_cancel_code=''

    def test_controller_database_diagnostic_is_sanitized(self):
        messages=[];error=sqlite3.OperationalError('https://private.invalid/?token=fixture-secret')
        error.sqlite_errorcode=517
        a.BUDGET.worker_deadline=time.monotonic()+240
        with patch('verification_remote.jobs',return_value=self.q),patch('verification_title.payload',return_value=self.payload),patch.object(self.q,'poll',side_effect=error),patch.object(a,'log',side_effect=messages.append):
            with self.assertRaises(Checkpoint):execute_title(a.__dict__,KEY)
        self.assertTrue(any('517' in m for m in messages))
        self.assertNotIn('private.invalid',''.join(messages));self.assertNotIn('fixture-secret',''.join(messages))
        with self.q.db() as c:self.assertEqual(c.execute('SELECT cancel_code FROM decode_jobs ORDER BY created DESC LIMIT 1').fetchone()[0],'controller_database')

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
        with self.q.db() as c:
            row=c.execute('SELECT deadline-created,execution_seconds FROM decode_jobs').fetchone()
            self.assertEqual(row[0],240)
            self.assertGreater(row[1],0)
            self.assertLessEqual(row[1],60)
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
        busy_cache=[False]
        class Handler(BaseHTTPRequestHandler):
            def log_message(self,*args):pass
            def do_POST(self):
                self.rfile.read(int(self.headers.get('Content-Length','0')))
                body=({'hash':HASH,'file_stats':[{'id':1,'path':'Fixture.2026.mp4','length':length}]} if self.path=='/torrents' else {'Hash':HASH,'Filled':0,'Readers':[{'Reader':1}] if busy_cache[0] else [],'Pieces':{}})
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
        import fcntl,hashlib
        locks=self.root/'locks'/'locks';locks.mkdir(parents=True)
        handle=(locks/hashlib.sha256(b'global-verification').hexdigest()).open('a')
        fcntl.flock(handle,fcntl.LOCK_EX);wait_seen=threading.Event()
        def release_lock():
            until=time.monotonic()+10
            while time.monotonic()<until:
                try:
                    if json.loads((state/'progress.json').read_text()).get('phase')=='waiting for backend lock':
                        wait_seen.set();time.sleep(.3);break
                except (OSError,ValueError):pass
                time.sleep(.05)
            fcntl.flock(handle,fcntl.LOCK_UN)
        releaser=threading.Thread(target=release_lock);releaser.start()
        try:subprocess.run([sys.executable,str(child),'--payload',str(payload),'--state',str(state),'--output',str(result)],env=env,check=True,capture_output=True,timeout=60)
        finally:releaser.join();handle.close()
        self.assertTrue(wait_seen.is_set())
        value=json.loads(result.read_text());self.assertEqual(value['status'],'ok',value)
        self.assertGreater(value['metrics']['active_seconds'],0)
        self.assertGreaterEqual(value['metrics']['lock_wait_seconds'],.25)
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
        busy_cache[0]=True
        failed=self.root/'checkpoint.json'
        subprocess.run([sys.executable,str(child),'--payload',str(payload),'--state',str(self.root/'busy-state'),'--output',str(failed)],env=env,check=True,capture_output=True,timeout=60)
        failure=json.loads(failed.read_text())
        self.assertEqual(failure['status'],'checkpoint')
        self.assertEqual(failure['diagnostic']['code'],'cache_attribution')
        self.assertNotIn('127.0.0.1',json.dumps(failure))
        with a.validation_attempt(True):a.finish_preparation(KEY,True,time.monotonic()+10,selected,info,proof,audio,conversion)
        with a.F.db() as c:
            row=c.execute('SELECT state,proof FROM validation WHERE key=?',(KEY,)).fetchone()
            self.assertEqual(row[0],'verified');self.assertEqual(json.loads(row[1])['source_backend'],'windows-torrent')

if __name__=='__main__':unittest.main()
