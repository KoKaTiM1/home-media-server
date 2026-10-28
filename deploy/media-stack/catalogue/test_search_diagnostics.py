"""Ready identities, provider failure, and source-only fresh-check fencing."""
import json,sqlite3,time,unittest
from pathlib import Path
from unittest.mock import patch
import test_features as fixtures

a=fixtures.a
KEY='movie:tt1234567'

class SearchDiagnosticsTests(unittest.TestCase):
    def setUp(self):
        fixtures.FeatureTests.setUp(self)
        self.proof={'version':5,'source_id':'a'*64,'checked_at':123}
        a.F.validation_result(KEY,True,proof=self.proof)
    def index(self):
        p=a.DATA/'imdb/catalogue.sqlite';p.parent.mkdir()
        with sqlite3.connect(p) as c:
            c.execute('CREATE TABLE titles(imdb TEXT PRIMARY KEY,kind TEXT,name TEXT,original TEXT,year INT,genres TEXT,votes INT)')
            c.executemany('INSERT INTO titles VALUES (?,?,?,?,?,?,?)',[
                ('tt1234567','movie','The Furious','Original title',2025,'Action',1),
                ('tt2345678','movie','The Furious','Another title',2026,'Action',100),
                ('tt3456789','movie','The Furious','Not available',2024,'Action',1000)])
        with a.F.db() as c:
            c.execute('INSERT INTO items VALUES (?,?,?,?,?)',(KEY,'movie','tt1234567',json.dumps({'id':'tt1234567','name':'The Furious','releaseInfo':'2026'}),'[]'))
        return p
    def state(self):
        with a.F.db() as c:return c.execute('SELECT state,proof,next_check FROM validation WHERE key=?',(KEY,)).fetchone()
    def test_ready_search_survives_provider_timeout_and_bulk_result_truncation(self):
        self.index()
        with patch.object(a.F,'fetch',side_effect=TimeoutError('https://private.invalid/?token=secret')),patch.object(a.F,'index_search',return_value=[]),patch.object(a.F,'decorate_posters',side_effect=lambda rows:rows):
            results=a.F.search('movie','The Furious')
        self.assertEqual([(r['id'],r['releaseInfo']) for r in results],[('tt1234567','2025')])
    def test_same_identity_provider_wrong_year_is_corrected_distinct_years_retained(self):
        self.index();a.F.validation_result('movie:tt2345678',True,proof=self.proof)
        provider=[{'id':'tt1234567','name':'The Furious','releaseInfo':'2026'}, {'id':'tt2345678','name':'The Furious','releaseInfo':'2026'}]
        with patch.object(a.F,'fetch',return_value={'metas':provider}),patch.object(a.F,'index_search',return_value=[]),patch.object(a.F,'decorate_posters',side_effect=lambda rows:rows):
            results=a.F.search('movie','The Furious')
        self.assertEqual({(r['id'],r['releaseInfo']) for r in results},{('tt1234567','2025'),('tt2345678','2026')})
        self.assertEqual([m['id'] for m in a.F.ready_search('movie','The Furious 2025')],['tt1234567'])
    def test_hebrew_alias_and_type_readiness_are_preserved(self):
        self.index()
        with a.F.db() as c:c.execute('INSERT INTO aliases VALUES (?,?,?)',('tt1234567','זעם','The Furious'))
        self.assertEqual([m['id'] for m in a.F.ready_search('movie','זעם')],['tt1234567'])
        self.assertEqual(a.F.ready_search('series','זעם'),[])
    def test_canonical_movie_year_preserves_real_release_date(self):
        self.index();raw={'name':'The Furious','releaseInfo':'2026','released':'2026-06-01'}
        result=a.F.canonical_movie_year('movie','tt1234567',raw)
        self.assertEqual(result['releaseInfo'],'2025');self.assertEqual(result['released'],'2026-06-01')
        self.assertEqual(raw['releaseInfo'],'2026')
    def test_transient_and_unknown_errors_never_invalidate_or_enqueue_source_check(self):
        errors=[TimeoutError(),a.Checkpoint(),a.ClientCancelled(),a.PlaybackBusy(),RuntimeError(),sqlite3.OperationalError(),
                a.urllib.error.HTTPError('https://private.invalid',503,'token=secret',{},None)]
        for error in errors:self.assertFalse(a.DIAG.recheck(KEY,error))
        self.assertEqual(self.state(),('verified',json.dumps(self.proof),0))
        with a.F.db() as c:self.assertEqual(c.execute('SELECT COUNT(*) FROM manual_rechecks').fetchone()[0],0)
    def test_confirmed_failure_pending_fence_only_background_pass_restores(self):
        a.remember('prepared:'+KEY+':1',{'old':True},3600)
        a.remember('verified-prepared:'+KEY,{'old':True},3600)
        self.assertTrue(a.DIAG.recheck(KEY,a.DecodeFailure()))
        self.assertFalse(a.DIAG.recheck(KEY,a.SourceStalled()))
        self.assertEqual(self.state()[0],'pending');self.assertEqual(json.loads(self.state()[1]),self.proof)
        self.assertNotIn(KEY,a.F.ready_keys());self.assertIsNone(a.cached('prepared:'+KEY+':1'))
        with patch.object(a,'resolve') as resolve:
            with self.assertRaises(a.Checkpoint):a.prepared(KEY)
            resolve.assert_not_called()
        self.assertIsNone(a.cached('verified-prepared:'+KEY))
        self.assertFalse(a.F.validation_result(KEY,True,proof=self.proof))
        self.assertEqual(self.state()[0],'pending')
        self.assertTrue(a.F.validation_result(KEY,True,proof=self.proof,background=True,attempt_started=time.time()+1))
        self.assertIn(KEY,a.F.ready_keys())
    def test_fresh_background_failure_keeps_title_unavailable(self):
        a.DIAG.recheck(KEY,a.SourceStalled())
        a.F.validation_result(KEY,False,background=True,attempt_started=time.time()+1)
        self.assertEqual(self.state()[0],'unavailable');self.assertNotIn(KEY,a.F.ready_keys())
    def test_unknown_background_request_error_retains_verified_proof_and_source(self):
        selected={'hash':'a'*40,'index':1,'filename':'fixture.mp4','length':100}
        with patch.object(a,'resolve',return_value=selected),patch.object(a,'probe',side_effect=RuntimeError('unclassified request fault')),patch.object(a.REL,'exclude') as excluded:
            with self.assertRaises(RuntimeError):a.prepared(KEY,background=True)
        excluded.assert_not_called();self.assertEqual(self.state()[0],'verified')
        self.assertEqual(json.loads(self.state()[1]),self.proof)
    def test_remote_source_process_without_bytes_is_checkpoint_not_decode_failure(self):
        selected={'hash':'a'*40,'index':1,'filename':'fixture.mp4','length':100}
        with patch.object(a.REL,'cache_state',return_value={}):
            with self.assertRaises(a.Checkpoint):
                a.REL.run(['ffprobe','-v','error','/fixture-not-present'],KEY,'video probe',selected,'fixture-no-bytes',time.monotonic()+5)
    def test_event_store_is_bounded_sanitized_and_expires(self):
        event={'key':KEY,'phase':'request','outcome':'failed','error_type':'HTTPError','error_category':'upstream_http',
               'http_status':503,'seconds':1,'url':'https://private.invalid/?token=secret','message':'secret'}
        with patch('playback_diagnostics.time.time',return_value=1):a.DIAG.record(event)
        for _ in range(205):a.DIAG.record(event)
        with a.F.db() as c:
            rows=c.execute('SELECT * FROM playback_events').fetchall()
        self.assertEqual(len(rows),200);self.assertNotIn('secret',json.dumps(rows));self.assertNotIn('private.invalid',json.dumps(rows))
        self.assertTrue(all(r[1]>1 for r in rows))

if __name__=='__main__':unittest.main()
