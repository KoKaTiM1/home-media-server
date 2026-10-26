import copy
import hashlib
import json
from pathlib import Path
import sqlite3
import ssl
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
import uuid

ROOT = Path(__file__).resolve().parents[1]
server_dir = ROOT/'server' if (ROOT/'server').is_dir() else ROOT/'metadata-worker/server'
worker_dir = ROOT/'metadata-worker/worker' if (ROOT/'metadata-worker/worker').is_dir() else Path('/opt/media-stack/metadata-worker/worker')
sys.path[:0] = [str(ROOT/'catalogue'), str(server_dir), str(worker_dir)]
import app
from features import Features
from metadata_jobs import MetadataJobs, InvalidSubmission, TTL
from coordinator import Coordinator, Server, PREFIX, clean_metadata
from worker import ControlAPI, Store, Worker, RemoteError, PROTOCOL

TOKEN = 'isolated-metadata-test-only-abcdefghijklmnopqrstuvwxyz'


def result(kind='series', imdb='tt0000001', **changes):
    return {'result_id': uuid.uuid4().hex, 'source': 'cinemeta', 'fetched_at': time.time(), 'status': 'ok',
            'metadata': {'id': imdb, 'type': kind, 'name': 'Example title', 'videos': []}, **changes}


class JobsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.jobs = MetadataJobs(Path(self.tmp.name)/'queue.sqlite')
        self.jobs.initialize('test-v1'); self.jobs.set_enabled(True)
        self.jobs.enqueue([('series','tt0000001',100),('movie','tt0000002',100)])

    def tearDown(self):
        self.tmp.cleanup()

    def lease(self):
        return self.jobs.claim('windows','windows-test','series')

    def test_server_windows_cannot_claim_same_title(self):
        barrier = threading.Barrier(2)
        claimed = []
        def run(owner, worker):
            barrier.wait()
            claimed.append(self.jobs.claim(owner,worker,'series','tt0000001' if owner=='server' else None))
        threads = [threading.Thread(target=run,args=args) for args in [('windows','windows-test'),('server','server')]]
        for thread in threads: thread.start()
        for thread in threads: thread.join(5)
        self.assertEqual(sum(x is not None for x in claimed),1)

    def test_receipt_and_handoff_idempotent_after_lease_completed(self):
        task = self.lease(); data = result()
        self.assertEqual(self.jobs.receive('windows','windows-test',task,data),'accepted')
        self.assertEqual(self.jobs.receive('windows','windows-test',task,data),'duplicate')
        self.assertEqual(len(self.jobs.pending_imports()),1)
        altered = copy.deepcopy(data); altered['metadata']['name'] = 'Changed'
        with self.assertRaises(InvalidSubmission):
            self.jobs.receive('windows','windows-test',task,altered)
        self.assertEqual(len(self.jobs.pending_imports()),1)

    def test_expired_first_result_rejected_without_handoff(self):
        task = self.lease()
        with self.jobs.db(True) as conn:conn.execute('UPDATE jobs SET expires=0')
        self.assertFalse(self.jobs.renew('windows','windows-test',task))
        self.assertEqual(self.jobs.receive('windows','windows-test',task,result()),'stale')
        self.assertEqual(self.jobs.pending_imports(),[])

    def test_generation_change_fences_old_lease(self):
        task = self.lease(); self.jobs.initialize('test-v2')
        self.assertEqual(self.jobs.receive('windows','windows-test',task,result()),'stale')

    def test_token_bound_to_first_windows_identity_and_partition(self):
        self.lease()
        with self.assertRaises(InvalidSubmission):self.jobs.claim('windows','different','series')
        with self.assertRaises(InvalidSubmission):self.jobs.claim('windows','windows-test','movie')

    def test_retry_cooldown_shared_with_server_and_not_source_failure(self):
        task = self.lease()
        r = result(status='retry',metadata=None,reason='provider_http_error',provider_status=429,retry_after_seconds=600)
        self.jobs.receive('windows','windows-test',task,r)
        self.assertIsNone(self.jobs.claim('server','server','movie','tt0000002'))
        self.assertEqual(self.jobs.pending_imports(),[])
        self.assertGreater(self.jobs.status()['provider_after'],time.time()+590)

    def test_server_fallback_after_windows_offline(self):
        self.jobs.seen()
        self.assertIsNone(self.jobs.claim('server','server','series','tt0000001'))
        with self.jobs.db(True) as conn:conn.execute('UPDATE clients SET seen=0')
        self.assertIsNotNone(self.jobs.claim('server','server','series','tt0000001'))

    def test_catalogue_page_and_title_claim_share_provider_slot(self):
        self.assertEqual(self.jobs.reserve_provider(),0)
        self.assertIsNone(self.jobs.claim('windows','windows-test','series'))
        self.assertGreater(self.jobs.reserve_provider(),0)

    def test_server_does_not_reuse_other_title_lease(self):
        self.jobs.claim('server','server','movie','tt0000002')
        with self.jobs.db(True) as conn:self.jobs.put(conn,'provider_slot',0)
        self.assertIsNone(self.jobs.claim('server','server','series','tt0000001'))

    def test_metadata_identity_and_artwork_validated(self):
        r = result()['metadata']
        r['poster']='https://127.0.0.1/private'
        r['videos']=[{'id':'tt0000001:1:1','season':1,'episode':1,'released':'bad-date','thumbnail':'https://example.org/image'}]
        r['streams']=[{'url':'https://example.org/arbitrary'}]
        clean=clean_metadata(r,'series','tt0000001')
        self.assertNotIn('poster',clean);self.assertNotIn('streams',clean)
        self.assertNotIn('released',clean['videos'][0]);self.assertNotIn('thumbnail',clean['videos'][0])


class ImportTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.root=Path(self.tmp.name)
        self.old=(app.DATA,app.LIBRARY,app.F,getattr(app.BUDGET,'metadata_background',False))
        app.DATA=self.root/'catalogue';app.DATA.mkdir();app.LIBRARY=self.root/'library';app.LIBRARY.mkdir()
        app.F=Features(app.__dict__);app.F.db().close()
        self.jobs=MetadataJobs(self.root/'helper.sqlite');self.jobs.initialize('test-v1');self.jobs.set_enabled(True)
        (app.DATA/'metadata-helper.enabled').write_text(json.dumps({'queue_path':str(self.jobs.path)}))
        with app.db() as conn:conn.execute("INSERT INTO validation(key,state,proof) VALUES ('series:tt0000001:1:1','pending',?)",(json.dumps({'fixture':'original-proof'}),))

    def tearDown(self):
        app.DATA,app.LIBRARY,app.F,app.BUDGET.metadata_background=self.old
        self.tmp.cleanup()

    def submit(self):
        self.jobs.enqueue([('series','tt0000001',100)])
        task=self.jobs.claim('windows','windows-test','series'); r=result()
        self.jobs.receive('windows','windows-test',task,r)
        return r

    def test_import_updates_cache_without_source_validation_or_library_files(self):
        self.submit();self.assertEqual(app.metadata_import(),1)
        self.assertEqual(app.cached('meta:series:tt0000001')['name'],'Example title')
        with app.db() as conn:
            state,proof=conn.execute('SELECT state,proof FROM validation').fetchone()
            self.assertEqual(state,'pending');self.assertEqual(json.loads(proof),{'fixture':'original-proof'})
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM items').fetchone()[0],0)
        self.assertEqual(list(app.LIBRARY.iterdir()),[])

    def test_newer_local_metadata_retained(self):
        self.submit()
        app.remember('meta:series:tt0000001',{'name':'Newer local'},TTL+100)
        self.assertEqual(app.metadata_import(),0)
        self.assertEqual(app.cached('meta:series:tt0000001')['name'],'Newer local')
        self.assertEqual(self.jobs.status()['imports'].get('kept_newer'),1)

    def test_crash_between_cache_commit_and_receipt_mark_is_safe(self):
        self.submit()
        with patch.object(MetadataJobs,'imported',side_effect=OSError('simulated crash')):
            with self.assertRaises(OSError):app.metadata_import()
        self.assertIsNotNone(app.cached('meta:series:tt0000001'))
        self.assertEqual(app.metadata_import(),0)
        self.assertEqual(self.jobs.pending_imports(),[])

    def test_background_series_waits_for_online_windows_instead_of_fetching(self):
        app.BUDGET.metadata_background=True;self.jobs.seen()
        with patch.object(app,'fetch',side_effect=AssertionError('Unexpected duplicate provider fetch')):
            with self.assertRaises(app.Checkpoint):app.metadata('series','tt0000001')

    def test_real_preparation_preserves_metadata_wait_signal(self):
        app.BUDGET.metadata_background=True;self.jobs.seen()
        def resolve(*args,**kwargs):return app.metadata('series','tt0000001')
        with patch.object(app,'resolve',side_effect=resolve):
            with self.assertRaises(app.MetadataPending):app.prepared('series:tt0000001:1:1',background=True)
        with app.REL.db() as conn:
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM preparation_work').fetchone()[0],0)

    def test_background_movie_uses_local_lease_and_normal_metadata_api(self):
        app.BUDGET.metadata_background=True
        with patch.object(app,'fetch',return_value={'meta':result(kind='movie',imdb='tt0000002')['metadata']}):
            self.assertEqual(app.metadata('movie','tt0000002')['id'],'tt0000002')
        self.assertEqual(self.jobs.status()['imports'].get('applied'),1)

    def test_generation_changed_handoff_cannot_be_imported(self):
        self.submit();self.jobs.initialize('new-schema')
        self.assertEqual(app.metadata_import(),0)
        self.assertIsNone(app.cached('meta:series:tt0000001'))
        self.assertEqual(self.jobs.status()['imports'].get('rejected'),1)

    def test_background_catalogue_page_honors_shared_provider_cooldown(self):
        app.BUDGET.metadata_background=True;self.jobs.provider_backoff(429)
        with patch.object(app.R,'urlopen',side_effect=AssertionError('Provider cooldown must prevent requests')):
            with self.assertRaises(app.Checkpoint):app.fetch('https://v3-cinemeta.strem.io/catalog/series/year/genre=2026.json')

    def test_metadata_wait_skips_to_next_audit_without_source_failure(self):
        app.BUDGET.metadata_background=True;self.jobs.seen();app.REL.db().close()
        movie='movie:tt0000002';series='series:tt0000001:1:1'
        with app.F.db() as conn:
            conn.execute("INSERT INTO validation(key,state) VALUES (?,'pending')",(movie,))
            conn.execute("INSERT INTO verification_audit(key,reason,scheduled,priority) VALUES (?,'test',?,1000)",(series,time.time()))
            conn.execute("INSERT INTO verification_audit(key,reason,scheduled,priority) VALUES (?,'test',?,500)",(movie,time.time()))
        visited=[]
        def prepare(key,**_):
            visited.append(key)
            if key==series:app.metadata('series','tt0000001')
            return {}
        with patch.object(app.F,'seed_validation'),patch.object(app.REL,'schedule'),patch.object(app.F,'consume_rechecks'),\
             patch.object(app.F,'repair_cached_series'),patch.object(app.F,'jf',return_value=[]),\
             patch.object(app,'prepared',side_effect=prepare),patch('features.time.sleep'):
            app.F.warm_availability(2)
        self.assertEqual(visited,[series,movie])
        with app.db() as conn:
            state,attempts,proof=conn.execute('SELECT state,attempts,proof FROM validation WHERE key=?',(series,)).fetchone()
            self.assertEqual((state,attempts),('pending',0));self.assertEqual(json.loads(proof),{'fixture':'original-proof'})

    def test_seed_is_bounded_metadata_work_and_honors_holds(self):
        app.REL.db().close()
        with app.F.db() as conn:
            conn.execute("INSERT INTO validation(key,state) VALUES ('series:tt0000003:1:1','pending')")
            conn.execute("INSERT INTO verification_holds VALUES ('series:tt0000003:1:1',?,'test')",(time.time()+600,))
            for key in ('series:tt0000001:1:1','series:tt0000003:1:1'):
                conn.execute("INSERT INTO verification_audit(key,reason,scheduled,priority) VALUES (?,'test',?,10)",(key,time.time()))
        self.assertEqual(self.jobs.seed(app.DATA/'catalogue.sqlite'),1)
        with self.jobs.db() as conn:
            self.assertEqual([r[0] for r in conn.execute('SELECT job_id FROM jobs')],['series:tt0000001'])

    def test_seed_filters_fresh_metadata_before_applying_window_limit(self):
        app.REL.db().close()
        with app.F.db() as conn:
            for number in range(105):
                imdb='tt'+str(1000000+number);key='series:'+imdb+':1:1'
                conn.execute("INSERT INTO validation(key,state) VALUES (?,'pending')",(key,))
                conn.execute("INSERT INTO verification_audit(key,reason,scheduled,priority) VALUES (?,'test',?,?)",(key,time.time(),200-number))
                if number<100:
                    conn.execute('INSERT INTO cache VALUES (?,?,?)',('meta:series:'+imdb,'{}',time.time()+3600))
        self.assertEqual(self.jobs.seed(app.DATA/'catalogue.sqlite'),5)

    def test_seed_does_not_prefetch_distant_discovery_while_audit_is_fresh(self):
        app.REL.db().close()
        with app.F.db() as conn:
            conn.execute("INSERT INTO verification_audit(key,reason,scheduled) VALUES ('series:tt0000001:1:1','test',?)",(time.time(),))
            conn.execute('INSERT INTO cache VALUES (?,?,?)',('meta:series:tt0000001','{}',time.time()+3600))
            conn.execute("INSERT INTO validation(key,state,priority) VALUES ('series:tt0000004:1:1','pending',100)")
        self.assertEqual(self.jobs.seed(app.DATA/'catalogue.sqlite'),0)


class NetworkTests(unittest.TestCase):
    def test_real_tls_claim_upload_lost_reply_restart_and_revocation(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);cert=root/'cert.pem';key=root/'key.pem'
            subprocess.run(['openssl','req','-x509','-newkey','rsa:2048','-nodes','-days','1','-subj','/CN=isolated-test',
                            '-keyout',str(key),'-out',str(cert)],check=True,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
            pin=hashlib.sha256(ssl.PEM_cert_to_DER_cert(cert.read_text())).hexdigest()
            cfg={'queue_path':str(root/'queue.sqlite'),'generation':'test-v1','bind':'127.0.0.1','port':0,'allowed_clients':['127.0.0.1']}
            coordinator=Coordinator(cfg,TOKEN);coordinator.jobs.set_enabled(True)
            coordinator.jobs.enqueue([('series','tt0000001',100)])
            context=ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER);context.load_cert_chain(cert,key)
            server=Server(coordinator,context);thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
            store=Store(root/'pc');config={'server_url':'https://127.0.0.1:'+str(server.server_port),'tls_sha256':pin,'token':TOKEN,'kind':'series'}
            api=ControlAPI(config);calls=[]
            def provider(kind,imdb):
                calls.append(imdb);return result()['metadata']
            try:
                client=Worker(config,store,api,provider);client.capabilities();client.step();client.step()
                original=api.request
                def lose_reply(endpoint,payload=None):
                    r=original(endpoint,payload)
                    if endpoint=='/result':raise OSError('simulated loss after real server commit')
                    return r
                api.request=lose_reply
                with self.assertRaises(OSError):client.step()
                store.close();store=Store(root/'pc');api.request=original
                client=Worker(config,store,api,provider);client.step()
                self.assertEqual(calls,['tt0000001']);self.assertIsNone(store.first())
                self.assertEqual(len(coordinator.jobs.pending_imports()),1)
                coordinator.token_digest=hashlib.sha256(('revoked-'+TOKEN).encode()).digest()
                with self.assertRaises(RemoteError) as failure:api.request('/status')
                self.assertEqual(failure.exception.status,401)
            finally:
                store.close();server.shutdown();server.server_close();thread.join(5)


if __name__=='__main__':unittest.main(verbosity=2)
