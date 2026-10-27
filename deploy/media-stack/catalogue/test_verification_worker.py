"""Lease fencing and real TLS/FFmpeg checks for the optional PC worker."""
import concurrent.futures
import hashlib
from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
import importlib.util
import json
import sqlite3
from pathlib import Path
import ssl
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import uuid
from unittest.mock import patch
from verification_jobs import DecodeJobs,InvalidJob,WorkerWait,PREFIX
from verification_media import execute,validate_result,MissingVideoFrames
from test_app import a

ROOT=Path(__file__).resolve().parents[1]
HELPER=ROOT/'helper' if (ROOT/'helper').is_dir() else ROOT/'verification-worker'
def load(name,path):
    spec=importlib.util.spec_from_file_location(name,path);module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module);return module
client=load('verify_client',HELPER/'verification_worker.py')
gateway=load('verify_gateway',HELPER/'coordinator.py')
KEY='movie:tt0000001';SID='a'*64;WORKER='isolated-windows';TOKEN='test-verification-token-only-'+('a'*40)

class QueueTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.q=DecodeJobs(Path(self.tmp.name)/'queue.sqlite');self.q.initialize();self.q.set('enabled',True);self.q.seen(WORKER)
    def submit(self):return self.q.submit(KEY,SID,'probe',{},'http://127.0.0.1:8092/raw/'+KEY,120)
    def result(self):return {'result_id':uuid.uuid4().hex,'status':'checkpoint'}
    def test_parallel_claims_return_same_exclusive_lease(self):
        job=self.submit()
        with concurrent.futures.ThreadPoolExecutor(max_workers=6) as pool:tasks=list(pool.map(lambda _:self.q.claim(WORKER),range(6)))
        self.assertEqual({t['job_id'] for t in tasks},{job});self.assertEqual(len({t['lease_id'] for t in tasks}),1)
    def test_four_enrolled_windows_workers_claim_distinct_operations(self):
        workers=[WORKER,'isolated-windows-2','isolated-windows-3','isolated-windows-4']
        for worker in workers[1:]:self.q.seen(worker)
        self.q.set('max_windows_jobs',4)
        self.assertEqual(self.q.status()['windows_slots_online'],4)
        with self.assertRaises(InvalidJob):self.q.seen('unapproved-fifth-worker')
        jobs=[self.submit()]
        for n in range(2,5):jobs.append(self.q.submit(f'movie:tt000000{n}',SID,'probe',{},f'http://127.0.0.1:8092/raw/movie:tt000000{n}',120))
        tasks=[self.q.claim(worker) for worker in workers]
        self.assertEqual([task['job_id'] for task in tasks],jobs)
        self.assertEqual(len({task['lease_id'] for task in tasks}),4)
        for worker,task in zip(workers,tasks):self.assertEqual(self.q.receive(worker,task,self.result()),'accepted')
    def test_lanes_above_guard_limit_are_not_queued(self):
        second='isolated-windows-2';self.q.seen(second);self.q.set('max_windows_jobs',2)
        self.submit()
        self.q.submit('movie:tt0000002',SID,'probe',{},'http://127.0.0.1:8092/raw/movie:tt0000002',120)
        with self.assertRaises(WorkerWait):
            self.q.submit('movie:tt0000003',SID,'probe',{},'http://127.0.0.1:8092/raw/movie:tt0000003',120)
    def test_prefetched_queue_cannot_exceed_active_limit_with_four_clients(self):
        self.q.set('max_windows_jobs',3);self.q.set('prefetch_per_lane',2)
        workers=[WORKER,'pc-two','pc-three','pc-four']
        for worker in workers:self.q.seen(worker)
        for n in range(6):self.q.submit(f'movie:tt00000{n:02}',SID,'probe',{},f'http://127.0.0.1:8092/raw/movie:tt00000{n:02}',120)
        self.assertEqual(self.q.status()['windows_queue_capacity'],6)
        with self.assertRaises(WorkerWait):self.submit()
        with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:tasks=list(pool.map(self.q.claim,workers))
        self.assertEqual(sum(t is not None for t in tasks),3)
        owner,task=next((w,t) for w,t in zip(workers,tasks) if t)
        idle=next(w for w,t in zip(workers,tasks) if t is None)
        self.assertEqual(self.q.receive(owner,task,self.result()),'accepted')
        following=self.q.claim(idle)
        self.assertIsNotNone(following)
        self.assertNotEqual(following['job_id'],task['job_id'])
        self.assertEqual(self.q.status()['jobs']['leased'],3)
    def test_waiting_claim_gets_fresh_job_and_stops_at_gate(self):
        with concurrent.futures.ThreadPoolExecutor() as pool:
            waiting=pool.submit(gateway.claim_wait,self.q,WORKER,2)
            time.sleep(.1);job=self.submit()
            self.assertEqual(waiting.result(timeout=1)['job_id'],job)
        self.q.finish(job);self.q.set('enabled',False)
        self.assertIsNone(gateway.claim_wait(self.q,WORKER,15))
    def test_queue_wait_does_not_consume_source_stall_clock(self):
        import verification_remote as remote
        from reliability import Checkpoint,SourceStalled
        selected={'hash':'a'*40,'index':1,'filename':'file.mkv','length':100}
        real_poll=self.q.poll;polls=[0]
        def poll(job):
            polls[0]+=1
            if polls[0]>=3:raise Checkpoint('test queue wait ended')
            return real_poll(job)
        with patch.object(remote,'jobs',return_value=self.q),patch.object(self.q,'poll',side_effect=poll),patch.object(remote,'STALL_SECONDS',.001),patch.object(remote.time,'sleep',side_effect=lambda _:None),patch.object(a.REL,'source_url',return_value='http://127.0.0.1:8092/raw/'+KEY),patch.object(a.REL,'cache_state',return_value={}),patch.object(a.REL,'checkpoint'),patch.object(a,'remaining',return_value=1),patch.object(a,'cached',return_value=None):
            with self.assertRaisesRegex(Checkpoint,'test queue wait ended'):
                remote.execute(a.__dict__,KEY,selected,'probe',{},time.monotonic()+5)
        self.assertEqual(polls[0],3)
    def test_progress_logs_suppress_idle_spam_but_show_new_jobs(self):
        messages=[];clock=[0]
        progress=client.ProgressLog(messages.append,lambda:clock[0])
        for at in (0,10,20,59,60):clock[0]=at;progress.write('waiting')
        self.assertEqual(messages,['waiting','waiting'])
        progress.write('video probe','job-one');progress.write('video probe','job-two')
        self.assertEqual(messages[-2:],['video probe','video probe'])
        self.assertIn('Paused by server',client.waiting_message({'enabled':False,'reason':'pilot complete'}))
        self.assertIn('source preparation',client.waiting_message({'enabled':True,'producer_activity':{'1':{'phase':'torrent_resolution'}}}))
        self.assertIn('decode slot',client.waiting_message({'enabled':True,'windows_capacity':3,'jobs':{'leased':3,'pending':2}}))
    def test_receipt_replay_after_consumption_and_conflict_rejected(self):
        job=self.submit();task=self.q.claim(WORKER);result=self.result()
        self.assertEqual(self.q.receive(WORKER,task,result),'accepted');self.q.finish(job,True)
        self.assertEqual(self.q.receive(WORKER,task,result),'duplicate')
        with self.assertRaises(InvalidJob):self.q.receive(WORKER,task,{**result,'status':'ok'})
    def test_expiry_cancel_generation_and_binding_fence_stale_workers(self):
        job=self.submit();task=self.q.claim(WORKER)
        with self.q.db(True) as c:c.execute('UPDATE decode_jobs SET controller=0 WHERE id=?',(job,))
        self.assertFalse(self.q.renew(WORKER,task));self.assertIsNone(self.q.source(job,task['lease_id']))
        self.assertEqual(self.q.receive(WORKER,task,self.result()),'stale')
        self.assertIsNone(self.q.claim('different-pc'))
        self.assertFalse(self.q.renew('different-pc',task))
        self.q.set('generation','changed');self.assertFalse(self.q.renew(WORKER,task))
    def test_stale_result_archive_can_grow_past_old_limit_and_replay(self):
        folder=Path(self.tmp.name)
        for _ in range(65):
            result=self.result()
            client.archive_stale_result(folder,{'operation':'probe'},result)
        self.assertEqual(len(list((folder/'quarantine').glob('*.json'))),65)
        last=json.loads(next((folder/'quarantine').glob('*.json')).read_text())
        client.archive_stale_result(folder,last['task'],last['result'])
        self.assertEqual(len(list((folder/'quarantine').glob('*.json'))),65)
    def test_worker_never_gets_upstream_url_and_server_cancel_revokes_stream(self):
        job=self.submit();task=self.q.claim(WORKER)
        self.assertNotIn('url',task);self.assertNotIn('http:',json.dumps(task))
        self.assertIsNotNone(self.q.source(job,task['lease_id']));self.q.finish(job)
        self.assertIsNone(self.q.source(job,task['lease_id']))
    def test_offline_worker_cannot_block_server(self):
        with self.q.db(True) as c:c.execute('UPDATE client SET seen=0')
        with self.assertRaises(RuntimeError):self.submit()
    def test_cannot_submit_commands_or_arbitrary_recipes(self):
        with self.assertRaises(ValueError):self.q.submit(KEY,SID,'command',{'shell':'whoami'},'http://127.0.0.1/raw/x',30)
    def test_progress_is_lease_bound_and_retains_only_fixed_outcomes(self):
        job=self.submit();task=self.q.claim(WORKER)
        self.assertTrue(self.q.renew(WORKER,task,'video probe'))
        with self.q.db() as c:
            self.assertEqual(c.execute('SELECT phase FROM decode_jobs WHERE id=?',(job,)).fetchone()[0],'video probe')
        with self.assertRaises(InvalidJob):self.q.renew(WORKER,task,'arbitrary log text')
        stale={**task,'lease_id':'0'*32}
        self.assertFalse(self.q.renew(WORKER,stale,'sample decode'))
        with self.q.db() as c:
            self.assertEqual(c.execute('SELECT phase FROM decode_jobs WHERE id=?',(job,)).fetchone()[0],'video probe')
        result={'result_id':uuid.uuid4().hex,'status':'checkpoint','reason':'transport'}
        self.assertEqual(self.q.receive(WORKER,task,result),'accepted');self.q.finish(job)
        with self.q.db() as c:
            row=c.execute('SELECT outcome,reason_code,url,result FROM decode_jobs WHERE id=?',(job,)).fetchone()
        self.assertEqual(tuple(row[:2]),('checkpoint','transport'));self.assertEqual(row[2:4],('' ,None))
        with self.assertRaises(InvalidJob):
            self.q.receive(WORKER,task,{'result_id':uuid.uuid4().hex,'status':'checkpoint','reason':'private error or URL'})
    def test_catalogue_lock_during_preparation_is_a_checkpoint(self):
        with patch.object(a,'prepare_locked',side_effect=sqlite3.OperationalError('database is locked')),patch.object(a.F,'verification_held',return_value=False),patch.object(a.F,'validation_result') as failure,patch.object(a.REL,'checkpoint') as checkpoint:
            with self.assertRaises(a.Checkpoint):a.prepared(KEY,background=True)
        failure.assert_not_called();checkpoint.assert_not_called()
    def test_claimed_stream_stall_uses_fallback_but_unstarted_job_checkpoints(self):
        import verification_remote as remote
        from reliability import SourceStalled,Checkpoint
        selected={'hash':'a'*40,'index':1,'filename':'file.mkv','length':100}
        original=self.q.submit
        for stream_started,expected in [(True,SourceStalled),(False,Checkpoint)]:
            def submit(*args):
                job=original(*args);task=self.q.claim(WORKER)
                if stream_started:self.q.mark_stream(job,task['lease_id'])
                return job
            with patch.object(remote,'jobs',return_value=self.q),patch.object(remote,'STALL_SECONDS',0),patch.object(self.q,'submit',side_effect=submit),patch.object(a.REL,'source_url',return_value='http://127.0.0.1:8092/raw/'+KEY),patch.object(a.REL,'cache_state',return_value={}),patch.object(a.REL,'checkpoint'),patch.object(a,'remaining',return_value=1),patch.object(a,'cached',return_value=None):
                with self.assertRaises(expected):remote.execute(a.__dict__,KEY,selected,'probe',{},time.monotonic()+30)
    def test_probe_without_exact_source_bytes_cannot_be_committed(self):
        import verification_remote as remote
        from reliability import Checkpoint
        selected={'hash':'a'*40,'index':1,'filename':'file.mkv','length':100};original=self.q.submit
        def submit(*args):
            job=original(*args);task=self.q.claim(WORKER)
            self.q.receive(WORKER,task,{'result_id':uuid.uuid4().hex,'status':'ok','data':{'info':{'streams':[{'codec_type':'video'}]}}})
            return job
        with patch.object(remote,'jobs',return_value=self.q),patch.object(self.q,'submit',side_effect=submit),patch.object(a.REL,'source_url',return_value='http://127.0.0.1:8092/raw/'+KEY),patch.object(a.REL,'cache_state',return_value={}),patch.object(a,'remaining',return_value=1),patch.object(a,'cached',return_value=None):
            with self.assertRaises(Checkpoint):remote.execute(a.__dict__,KEY,selected,'probe',{},time.monotonic()+30)
    def test_server_key_claims_are_exclusive_and_expire(self):
        with patch.object(a,'DATA',Path(self.tmp.name)/'catalogue'):
            a.F.db().close()
            with concurrent.futures.ThreadPoolExecutor(max_workers=2) as p:
                claims=list(p.map(lambda owner:a.F.claim_verification(KEY,owner),['server','windows']))
            self.assertEqual(sum(bool(v) for v in claims),1)
            token=next(v for v in claims if v)
            self.assertTrue(a.F.renew_verification(KEY,token));a.F.release_verification(KEY,token)
            self.assertIsNotNone(a.F.claim_verification(KEY,'server'))

class RealDecoderTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp=tempfile.TemporaryDirectory();cls.root=Path(cls.tmp.name)
        cls.clip=cls.root/'clip.mkv'
        subprocess.run(['ffmpeg','-nostdin','-v','error','-threads','1','-filter_threads','1','-f','lavfi','-i','testsrc2=size=320x180:rate=12','-f','lavfi','-i','sine=frequency=400:sample_rate=48000','-t','16','-c:v','libx264','-threads','1','-preset','ultrafast','-c:a','ac3',str(cls.clip)],check=True)
        subprocess.run(['openssl','req','-x509','-newkey','rsa:2048','-nodes','-keyout',str(cls.root/'key.pem'),'-out',str(cls.root/'cert.pem'),'-subj','/CN=isolated-verification','-days','1'],check=True,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
    @classmethod
    def tearDownClass(cls):cls.tmp.cleanup()
    def local_run(self,command,phase):
        r=subprocess.run(command,capture_output=True,timeout=20)
        self.assertEqual(r.returncode,0,r.stderr.decode());return r
    def params(self,conversion):return {'position':6,'seconds':2,'video_index':0,'video_codec':'h264','audio_index':1,'conversion':conversion}
    def test_real_direct_and_fresh_aac_lc_decode(self):
        for conversion in (False,True):
            params=self.params(conversion)
            result=execute('sample',params,str(self.clip),self.local_run,self.root)
            proof=validate_result('sample',params,result)
            self.assertGreater(proof['video_frames'],10);self.assertGreater(proof['audio_frames'],10)
    def test_zero_frame_or_fake_conversion_cannot_pass(self):
        with self.assertRaises(MissingVideoFrames):validate_result('sample',self.params(False),{'crc':'#media_type 0: video\n#tb 0: 1/12\n'})
        with self.assertRaises(RuntimeError):validate_result('sample',self.params(True),{'crc':'','conversion_streams':[]})
    def test_full_tls_gateway_worker_decode_and_lost_ack_replay(self):
        clip=self.clip.read_bytes()
        class Upstream(BaseHTTPRequestHandler):
            def log_message(self,*args):pass
            def do_HEAD(self):self.handle_clip(True)
            def do_GET(self):self.handle_clip(False)
            def handle_clip(self,head):
                start,end=0,len(clip)-1
                if self.headers.get('Range'):
                    part=self.headers['Range'][6:].split('-');start=int(part[0] or 0);end=int(part[1]) if part[1] else end
                end=min(end,len(clip)-1)
                self.send_response(206 if self.headers.get('Range') else 200);self.send_header('Content-Type','video/x-matroska');self.send_header('Content-Length',str(end-start+1));self.send_header('Accept-Ranges','bytes')
                if self.headers.get('Range'):self.send_header('Content-Range',f'bytes {start}-{end}/{len(clip)}')
                self.end_headers()
                if not head:
                    from urllib.parse import urlsplit,parse_qs
                    token=parse_qs(urlsplit(self.path).query).get('progress',[None])[0]
                    if token:a.remember('sample-progress:'+token,{'bytes':end-start+1},3600)
                    try:self.wfile.write(clip[start:end+1])
                    except (BrokenPipeError,ConnectionResetError):pass
        upstream=ThreadingHTTPServer(('127.0.0.1',0),Upstream);threading.Thread(target=upstream.serve_forever,daemon=True).start();self.addCleanup(upstream.server_close);self.addCleanup(upstream.shutdown)
        with tempfile.TemporaryDirectory() as directory:
            q=DecodeJobs(Path(directory)/'queue.sqlite');q.initialize();q.set('enabled',True);q.seen(WORKER)
            server=gateway.Server(('127.0.0.1',0),gateway.Handler);server.allowed={'127.0.0.1'};server.slots=threading.BoundedSemaphore(12);server.jobs=q;server.bridge='http://127.0.0.1:'+str(upstream.server_port)
            server.auth=hashlib.sha256(('Bearer '+TOKEN).encode()).digest();ctx=ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER);ctx.load_cert_chain(self.root/'cert.pem',self.root/'key.pem');server.context=ctx
            threading.Thread(target=server.serve_forever,daemon=True).start();self.addCleanup(server.server_close);self.addCleanup(server.shutdown)
            pin=hashlib.sha256(ssl.PEM_cert_to_DER_cert((self.root/'cert.pem').read_text())).hexdigest()
            api=client.API({'server_url':'https://127.0.0.1:'+str(server.server_port),'token':TOKEN,'tls_sha256':pin})
            for op,params in [('probe',{}),('sample',self.params(True))]:
                job=q.submit(KEY,SID,op,params,server.bridge+'/raw/'+KEY,120)
                task=api.request('/claim',{'worker_id':WORKER})['task']
                phase='video probe' if op=='probe' else 'sample decode'
                self.assertTrue(api.request('/renew',{'worker_id':WORKER,'task':task,'phase':phase})['active'])
                with q.db() as conn:
                    self.assertEqual(conn.execute('SELECT phase FROM decode_jobs WHERE id=?',(job,)).fetchone()[0],phase)
                result=client.run_task(api,WORKER,task,lambda message:None);self.assertEqual(result['status'],'ok',result)
                proof=validate_result(op,params,result['data'])
                if op=='sample':self.assertGreater(proof['video_frames'],10)
                result['result_id']=uuid.uuid4().hex
                request={'worker_id':WORKER,'task':task,'result':result}
                self.assertEqual(api.request('/result',request)['ack'],'accepted');q.finish(job,True)
                self.assertEqual(api.request('/result',request)['ack'],'duplicate')
            # Drive the actual trusted prepared() wrapper while the PC-side
            # worker decodes probe + both fresh converted samples over TLS.
            selected={'hash':'b'*40,'index':1,'filename':'Example.2026.mkv','length':len(clip)}
            data=Path(directory)/'catalogue';library=Path(directory)/'library';secret=Path(directory)/'signing';secret.write_text('isolated-signing-value')
            errors=[];done=threading.Event()
            def pc_loop():
                while not done.is_set():
                    try:
                        task=api.request('/claim',{'worker_id':WORKER}).get('task')
                        if not task:done.wait(.05);continue
                        result=client.run_task(api,WORKER,task,lambda message:None);result['result_id']=uuid.uuid4().hex
                        api.request('/result',{'worker_id':WORKER,'task':task,'result':result})
                    except Exception as error:errors.append(type(error).__name__);return
            original_url=a.REL.source_url
            def source(route,key,selection,token=None):
                if route=='raw':return server.bridge+'/raw/'+key+'?progress='+token
                return original_url(route,key,selection,token)
            with patch.multiple(a,DATA=data,LIBRARY=library,SECRET=secret),patch.object(a,'resolve',return_value=selected),patch.object(a.REL,'source_url',side_effect=source):
                data.mkdir();library.mkdir();(data/'verification-helper.json').write_text(json.dumps({'queue_path':str(q.path)}))
                a.F.enqueue(KEY);a.BUDGET.remote_verification=True
                thread=threading.Thread(target=pc_loop,daemon=True);thread.start()
                try:
                    prepared=a.prepared(KEY,background=True)
                    with a.F.db() as c:state,raw=c.execute('SELECT state,proof FROM validation WHERE key=?',(KEY,)).fetchone()
                    proof=json.loads(raw)
                    self.assertEqual(state,'verified');self.assertEqual(proof['verification_worker'],'windows')
                    self.assertEqual(len(proof['samples']),2);self.assertTrue(proof['audio_conversion'])
                    self.assertTrue(prepared['AudioConversionRequired']);self.assertFalse(errors)
                finally:done.set();thread.join(timeout=5);a.BUDGET.remote_verification=False
            bad=client.API({'server_url':'https://127.0.0.1:'+str(server.server_port),'token':TOKEN,'tls_sha256':'f'*64})
            with self.assertRaises(client.TransportWait):bad.request('/status')

if __name__=='__main__':unittest.main()
