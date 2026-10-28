"""Speed proofs cannot be earned by cached data or mismatched decode evidence."""
import json,time,unittest,threading
from unittest.mock import patch
from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
import test_features as fixtures
from reliability import SourceTooSlow,Checkpoint
from test_durable_playback import DurablePlaybackTests,SELECTED,KEY,INFO

a=fixtures.a
class SpeedTests(DurablePlaybackTests):
    # Inherit exact-source reuse/invalidation regressions as well.
    def speed_fixture(self,cached=0):
        selected={**SELECTED,'length':100*1024**2}
        info={**INFO,'format':{'duration':1000}}
        cache={'Hash':selected['hash'],'Filled':cached,'Pieces':{},'Readers':[]}
        return selected,info,cache
    def test_speed_only_upgrade_keeps_decode_time_and_samples(self):
        old=self.seed();speed={'policy':1,'mode':'torrent','windows':[{},{}],'checked_at':time.time()}
        with patch.object(a.REL,'sample',side_effect=AssertionError('redecoded')),patch.object(a.REL,'speed_verify',return_value=speed):
            p=a.REL.verify(KEY,SELECTED,INFO,1,False,time.monotonic()+60)
        self.assertEqual(p['version'],6);self.assertEqual(p['samples'],old['samples'])
        self.assertEqual(p['decode_checked_at'],old['checked_at']);self.assertEqual(p['speed'],speed)
    def test_changed_file_or_audio_requires_decode(self):
        self.seed()
        for changed,audio,conversion in (({**SELECTED,'hash':'b'*40},1,False),(SELECTED,2,False),(SELECTED,1,True)):
            self.assertIsNone(a.REL.decode_proof(KEY,changed,audio,conversion))
    def test_manual_recheck_prevents_decode_reuse(self):
        self.seed()
        with a.F.db() as c:c.execute("INSERT OR REPLACE INTO manual_rechecks VALUES (?,?,?,'pending')",(KEY,time.time(),'fresh'))
        self.assertIsNone(a.REL.decode_proof(KEY,SELECTED,1,False))
    def test_full_speed_pass_uses_uncached_lower_bound(self):
        selected,info,cache=self.speed_fixture(1024**2)
        def read(key,sel,start,size,deadline):return {'offset':start,'bytes':size,'seconds':1,'first_byte_seconds':.1}
        with patch.object(a.REL,'speed_read',side_effect=read),patch.object(a.REL,'cache_state',return_value=cache):
            speed=a.REL.speed_verify(KEY,selected,info,time.monotonic()+60)
        self.assertEqual(len(speed['windows']),2)
        self.assertEqual(speed['windows'][0]['uncached_bytes_lower_bound'],4*1024**2)
    def test_cached_heavy_source_checkpoints_without_false_failure(self):
        selected,info,cache=self.speed_fixture(32*1024**2)
        with patch.object(a.REL,'speed_read',return_value={'first_byte_seconds':.1}),patch.object(a.REL,'cache_state',return_value=cache):
            with self.assertRaises(Checkpoint):a.REL.speed_verify(KEY,selected,info,time.monotonic()+60)
    def test_slow_delivery_rejected(self):
        selected,info,cache=self.speed_fixture()
        def read(key,sel,start,size,deadline):return {'offset':start,'bytes':size,'seconds':100,'first_byte_seconds':.1}
        with patch.object(a.REL,'speed_read',side_effect=read),patch.object(a.REL,'cache_state',return_value=cache):
            with self.assertRaises(SourceTooSlow):a.REL.speed_verify(KEY,selected,info,time.monotonic()+60)
    def test_other_reader_or_missing_cache_checkpoints(self):
        selected,info,cache=self.speed_fixture()
        for value in ({}, {**cache,'Readers':[{}]}):
            with patch.object(a.REL,'speed_read',return_value={'first_byte_seconds':.1}),patch.object(a.REL,'cache_state',return_value=value):
                with self.assertRaises(Checkpoint):a.REL.speed_verify(KEY,selected,info,time.monotonic()+.01)
    def test_slow_source_exclusion_is_six_hours(self):
        self.seed();before=time.time();a.REL.exclude(KEY,SELECTED,21600)
        with a.F.db() as c:until=c.execute('SELECT excluded FROM source_choices WHERE key=?',(KEY,)).fetchone()[0]
        self.assertGreaterEqual(until,before+21600)
    def test_real_http_range_and_wrong_content_range(self):
        class Handler(BaseHTTPRequestHandler):
            bad=False
            def log_message(self,*args):pass
            def do_GET(self):
                self.send_response(206);self.send_header('Content-Range','bytes '+('5-14/100' if self.bad else '10-19/100'));self.send_header('Content-Length','10');self.end_headers();self.wfile.write(b'0123456789')
        server=ThreadingHTTPServer(('127.0.0.1',0),Handler);thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
        try:
            a.REL._speed_required=1
            with patch.object(a,'upstream',return_value='http://127.0.0.1:'+str(server.server_port)+'/stream'),patch.object(a,'torr_headers',return_value={}):
                result=a.REL.speed_read(KEY,{**SELECTED,'length':100},10,10,time.monotonic()+5);self.assertEqual(result['bytes'],10)
                Handler.bad=True
                with self.assertRaises(Checkpoint):a.REL.speed_read(KEY,{**SELECTED,'length':100},10,10,time.monotonic()+5)
        finally:server.shutdown();server.server_close();thread.join()
    def test_new_source_runs_both_decode_samples(self):
        self.seed();new={**SELECTED,'hash':'b'*40}
        def sample(key,sel,info,audio,conv,pos,sec,end):return {'position':pos,'seconds':sec,'video_frames':100,'audio_frames':100}
        with patch.object(a.REL,'speed_verify',return_value={'mode':'torrent'}),patch.object(a.REL,'sample',side_effect=sample) as decode:
            p=a.REL.verify(KEY,new,INFO,1,False,time.monotonic()+60)
        self.assertEqual(decode.call_count,2);self.assertEqual(p['source_hash'],new['hash'])
    def test_speed_migration_is_idempotent_and_keeps_titles_visible(self):
        self.seed()
        with a.F.db() as c:c.execute("INSERT OR REPLACE INTO cache VALUES ('verification-v5-audit','true',?)",(time.time()+999,))
        a.REL.schedule()
        with a.F.db() as c:
            self.assertEqual(c.execute('SELECT state FROM validation WHERE key=?',(KEY,)).fetchone()[0],'verified')
            self.assertEqual(c.execute('SELECT reason,completed FROM verification_audit WHERE key=?',(KEY,)).fetchone(),('speed-v6',0.0))
            c.execute("UPDATE verification_audit SET completed=1,result='passed' WHERE key=?",(KEY,))
        a.REL.schedule()
        with a.F.db() as c:self.assertEqual(c.execute('SELECT completed FROM verification_audit WHERE key=?',(KEY,)).fetchone()[0],1)
    def test_invalid_speed_proof_cannot_be_reused_for_playback(self):
        p=self.seed()
        with a.F.db() as c:
            p.update(version=6,speed={'policy':1,'checked_at':time.time()})
            c.execute('UPDATE validation SET proof=? WHERE key=?',(json.dumps(p),KEY))
            raw=json.loads(c.execute('SELECT value FROM playback WHERE key=?',(KEY,)).fetchone()[0]);raw['verification_version']=6
            c.execute('UPDATE playback SET value=? WHERE key=?',(json.dumps(raw),KEY))
        self.assertIsNone(a.durable_playback_evidence(KEY))
    def test_prior_speed_failure_does_not_discard_good_decode_evidence(self):
        old=self.seed();a.F.validation_result(KEY,False,reason='Video preflight failed (SourceTooSlow)')
        self.assertEqual(a.REL.decode_proof(KEY,SELECTED,1,False)['samples'],old['samples'])
        a.F.validation_result(KEY,False,reason='Video preflight failed (DecodeFailure)')
        self.assertIsNone(a.REL.decode_proof(KEY,SELECTED,1,False))
