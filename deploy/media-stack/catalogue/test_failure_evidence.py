"""Failure evidence survives exhaustion without changing operational decisions."""
import copy,importlib.util,json,os,sqlite3,tempfile,time,unittest,uuid
from contextlib import ExitStack
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from source_failure_diagnostics import FailureTrace,scope,record,tag,sanitize
from reliability import SourceFailure,SourceTooSlow,DecodeFailure,Checkpoint
from verification_diagnostics import DiagnosticLedger
HASH='a'*40
ROOT={'category':'speed_rejected','failure_type':'SourceTooSlow','phase':'speed verification','code':'speed_read_timeout','source_hash':HASH}
class TraceTests(unittest.TestCase):
 def test_exhaustion_preserves_root_but_cancellation_does_not_hide_terminal(self):
  t=FailureTrace();t.record(SourceTooSlow('private-url','speed_read_timeout'),'speed verification',HASH)
  t.record(SourceTooSlow('private-url','speed_read_timeout'),'title validation',HASH)
  e=tag(SourceFailure('private-secret'),'source_candidates_exhausted')
  self.assertEqual(t.evidence(e)['root_cause'],ROOT);self.assertEqual(len(t.rows),1)
  self.assertEqual(t.evidence(Checkpoint('private','local_work_budget'))['root_cause']['code'],'local_work_budget')
 def test_scope_restores_thread_local_state_and_never_changes_error_code(self):
  b=SimpleNamespace();error=tag(SourceFailure('secret'),'source_file_unmatched')
  with self.assertRaises(SourceFailure):
   with scope(b):record(b,error,'torrent resolution',HASH);raise error
  self.assertIsNone(b.source_failure_trace);self.assertFalse(hasattr(error,'code'))
  self.assertEqual(error.source_failure_evidence['root_cause']['code'],'source_file_unmatched')
 def test_bounded_history_and_malformed_or_forged_evidence(self):
  t=FailureTrace()
  for n in range(50):t.record(SourceTooSlow('secret','speed_read_timeout'),'speed verification',f'{n:040x}')
  self.assertEqual(len(t.rows),20)
  safe=sanitize({'root_cause':{**ROOT,'url':'secret','message':'secret'},'source_failures':[ROOT,{**ROOT,'code':[]},{**ROOT,'category':{}},'secret']},{HASH})
  self.assertEqual(safe,{'root_cause':ROOT,'source_failures':[ROOT]});self.assertNotIn('secret',json.dumps(safe))
  self.assertEqual(sanitize({'root_cause':ROOT},{'b'*40}),{})
 def test_completed_attempt_cannot_leak_history_into_the_next_attempt(self):
  b=SimpleNamespace()
  with scope(b):record(b,SourceTooSlow('fixture','speed_read_timeout'),'speed verification',HASH)
  error=tag(SourceFailure('fixture'),'source_candidates_exhausted')
  with self.assertRaises(SourceFailure):
   with scope(b):raise error
  self.assertEqual(error.source_failure_evidence['root_cause']['code'],'source_candidates_exhausted')
  self.assertNotIn('speed_read_timeout',json.dumps(error.source_failure_evidence))
 def test_root_buckets_are_separate_and_replay_safe_and_clear_on_success(self):
  with tempfile.TemporaryDirectory() as folder:
   ledger=DiagnosticLedger(Path(folder)/'history.sqlite')
   e={'scope':'worker_job','key':'movie:tt0000001','finished':1,'outcome':'checkpoint','category':'transport','code':'unclassified','root_cause':ROOT,'source_failures':[ROOT]}
   ledger.ingest([('one',json.dumps(e))]);ledger.ingest([('one',json.dumps(e))])
   with ledger.connect() as c:
    self.assertEqual(c.execute("SELECT count FROM buckets WHERE bucket='worker_job/checkpoint/transport/unclassified'").fetchone()[0],1)
    self.assertEqual(c.execute("SELECT count FROM buckets WHERE bucket='root/worker_job/speed_rejected/speed_read_timeout'").fetchone()[0],1)
   ledger.ingest([('two',json.dumps({**e,'finished':2,'outcome':'ok','category':'','code':'','root_cause':{'url':'secret'},'source_failures':['secret']}))])
   with ledger.connect() as c:
    self.assertEqual(c.execute("SELECT count FROM buckets WHERE bucket='root/worker_job/speed_rejected/speed_read_timeout'").fetchone()[0],0)
    self.assertNotIn('secret',c.execute("SELECT payload FROM latest").fetchone()[0])

class EndToEndTests(unittest.TestCase):
 def setUp(self):
  from test_verification_title import TitleTests,a
  self.fixture=TitleTests();self.fixture.setUp();self.addCleanup(self.fixture.doCleanups);self.a=a
 def child(self,error,status_override=None,expire_during_failure=False):
  from reliability import Reliability
  a=self.a;t=self.fixture
  spec=importlib.util.spec_from_file_location('diagnostic_child',Path(__file__).parents[1]/'verification-worker/local_verifier.py');m=importlib.util.module_from_spec(spec);spec.loader.exec_module(m)
  info={'streams':[{'index':0,'codec_type':'video','codec_name':'h264','pix_fmt':'yuv420p'},{'index':1,'codec_type':'audio','codec_name':'aac','profile':'LC','tags':{'language':'eng'}}],'format':{'duration':'600'}}
  status=status_override or {'hash':HASH,'file_stats':[{'id':1,'path':'Fixture.2026.mp4','length':10000000}]}
  import sys
  actual_clock=time.monotonic;expired=[False]
  def fail(*args,**kwargs):
   if expire_during_failure:expired[0]=True
   raise error
  output=t.root/'out.json'
  with ExitStack() as s:
   s.enter_context(patch.dict(sys.modules,{'app':a}));s.enter_context(patch.dict(os.environ,{'LOCAL_TORRENT_STATE':str(t.root/'torrent-state')}))
   for name in ['DATA','REL','TORR','torr_headers','verification_candidates','log']:s.enter_context(patch.object(a,name,getattr(a,name)))
   s.enter_context(patch.object(a,'torr',return_value=status));s.enter_context(patch.object(a,'probe',return_value=info));s.enter_context(patch.object(Reliability,'speed_verify',side_effect=fail))
   if expire_during_failure:s.enter_context(patch.object(m.time,'monotonic',side_effect=lambda:actual_clock()+(11 if expired[0] else 0)))
   m.verify(t.payload,t.root/'local',output,10)
  return json.loads(output.read_text())
 def test_real_preparation_exhaustion_preserves_speed_reason_and_legacy_control(self):
  result=self.child(SourceTooSlow('secret-url','speed_read_timeout'));d=result['diagnostic']
  self.assertEqual(result['status'],'checkpoint');self.assertEqual(result['reason'],'transport')
  self.assertEqual({k:d[k] for k in ('category','failure_type','code')},{'category':'transport','failure_type':'SourceFailure','code':'unclassified'})
  self.assertEqual(d['root_cause'],ROOT)
  self.assertNotIn('secret-url',json.dumps(d))
 def test_decode_reason_survives_exhaustion(self):
  e=DecodeFailure('secret');e.code='ffmpeg_invalid_data';result=self.child(e)
  self.assertEqual(result['diagnostic']['root_cause']['code'],'ffmpeg_invalid_data')
  self.assertEqual(result['diagnostic']['code'],'unclassified')
 def test_identity_rejection_is_tagged_without_changing_the_legacy_error(self):
  status={'hash':'b'*40,'file_stats':[{'id':1,'path':'Fixture.2026.mp4','length':10000000}]}
  result=self.child(SourceTooSlow('unused','speed_read_timeout'),status)
  self.assertEqual(result['diagnostic']['root_cause']['code'],'source_identity_mismatch')
  self.assertEqual(result['diagnostic']['code'],'unclassified')
 def test_expiring_child_budget_overrides_attached_wrapper_evidence(self):
  result=self.child(SourceTooSlow('fixture','speed_read_timeout'),expire_during_failure=True)
  self.assertEqual(result['diagnostic']['code'],'local_work_budget')
  self.assertEqual(result['diagnostic']['root_cause']['code'],'local_work_budget')
 def test_actual_budget_termination_keeps_its_own_cause(self):
  result=self.child(Checkpoint('fixture','local_work_budget'))
  self.assertEqual(result['diagnostic']['root_cause']['code'],'local_work_budget')
  self.assertEqual(result['diagnostic']['code'],'local_work_budget')
 def test_richer_diagnostics_do_not_change_fallback_or_exclusions(self):
  from verification_title import execute_title
  a=self.a;t=self.fixture;observed=[]
  for extra in ({},{'root_cause':ROOT,'source_failures':[ROOT]}):
   a.BUDGET.remote_verification=True;a.BUDGET.worker_deadline=time.monotonic()+60
   d={'category':'transport','failure_type':'SourceFailure','phase':'torrent resolution','code':'unclassified',**extra}
   calls=[]
   with patch('verification_remote.jobs',return_value=t.q),patch('verification_title.payload',return_value=t.payload),patch.object(t.q,'poll',return_value=('result',{'status':'checkpoint','reason':'transport','diagnostic':d})),patch.object(a,'prepared',side_effect=lambda *x,**kw:calls.append(a.BUDGET.remote_verification)):
    self.assertTrue(execute_title(a.__dict__,t.payload['key']))
   observed.append((calls,a.cached('source-excluded:'+t.payload['key']+':'+HASH),a.cached('mediafusion-needed:'+t.payload['key'])))
  self.assertEqual(observed,[([False],None,None)]*2)

class ReceiverTests(unittest.TestCase):
 def test_assignment_binding_uses_server_payload_and_consumed_replay_is_idempotent(self):
  from verification_jobs import DecodeJobs,InvalidJob
  for provider in ('torrentio','mediafusion','other'):
   with self.subTest(provider=provider),tempfile.TemporaryDirectory() as folder:
    q=DecodeJobs(Path(folder)/'queue.sqlite');q.initialize();q.set('enabled',True);q.seen('pc')
    q.set('local_torrent_enabled',True);q.set('local_torrent_permitted',True);q.backend_health('pc',True,'test-backend')
    params={'schema':1,'key':'movie:tt0000001','meta':{},'aliases':[],'prior':{},'provider':provider,'candidates':[{'infoHash':HASH,'provider':provider}]}
    job=q.submit('movie:tt0000001','c'*64,'verify_title',params,'',120);task=q.claim('pc',['local-torrent-v1'])
    # The received task's params are untrusted; only stored assignment counts.
    task['params']={'candidates':[{'infoHash':'b'*40}]}
    forged={**ROOT,'source_hash':'b'*40}
    value={'result_id':uuid.uuid4().hex,'status':'checkpoint','reason':'transport','diagnostic':{'category':'transport','failure_type':'SourceFailure','code':'unclassified','root_cause':ROOT,'source_failures':[ROOT,forged]}}
    self.assertEqual(q.receive('pc',task,value),'accepted')
    with q.db() as c:
     d=json.loads(c.execute('SELECT result FROM decode_jobs WHERE id=?',(job,)).fetchone()[0])['diagnostic']
     self.assertEqual(d['root_cause'],ROOT);self.assertEqual(d['source_failures'],[ROOT])
    q.finish(job)
    self.assertEqual(q.receive('pc',task,value),'duplicate')
    changed=copy.deepcopy(value);changed['diagnostic']['root_cause']['code']='speed_startup_timeout'
    with self.assertRaises(InvalidJob):q.receive('pc',task,changed)
    with q.db() as c:
     event=json.loads(c.execute('SELECT payload FROM diagnostic_outbox').fetchone()[0]);self.assertEqual(event['root_cause'],ROOT)
    q.flush_diagnostics()
    c=sqlite3.connect(Path(folder)/'verification-diagnostics.sqlite');self.assertEqual(c.execute('SELECT count(*) FROM receipts').fetchone()[0],1);c.close()
if __name__=='__main__':unittest.main()
