"""Real worker cleanup stays bounded and preserves legacy matching/proof state."""
import json,sqlite3,time,unittest
from contextlib import ExitStack
from unittest.mock import patch
import test_verification_title as fixture
a=fixture.a;KEY=fixture.KEY
class CacheAndDiagnosticTests(unittest.TestCase):
 def setUp(self):
  self.t=fixture.TitleTests();self.t.setUp();self.addCleanup(self.t.doCleanups)
  attrs=('deadline','cancel','worker_deadline','speculative','remote_verification','remote_phase','validation_background','first_sweep_attempt')
  old={n:(hasattr(a.BUDGET,n),getattr(a.BUDGET,n,None)) for n in attrs}
  def restore():
   for n,(existed,value) in old.items():
    if existed:setattr(a.BUDGET,n,value)
    elif hasattr(a.BUDGET,n):delattr(a.BUDGET,n)
  self.addCleanup(restore)
  a.BUDGET.deadline=None;a.BUDGET.cancel=None;a.BUDGET.worker_deadline=time.monotonic()+120
  a.BUDGET.speculative=False;a.BUDGET.remote_verification=False;a.BUDGET.remote_phase='source preparation'
  a.BUDGET.validation_background=False;a.BUDGET.first_sweep_attempt=False
  with a.F.db() as c:
   c.execute("INSERT INTO validation(key,state,priority,checked,proof) VALUES (?,'pending',1,123,?)",(KEY,json.dumps({'fixture':'preserved'})))
 def warm(self,error):
  with ExitStack() as s:
   for name in ('seed_validation','repair_cached_series'):s.enter_context(patch.object(a.F,name))
   s.enter_context(patch.object(a.REL,'schedule'));s.enter_context(patch.object(a.F,'foreground_busy',return_value=False))
   s.enter_context(patch.object(a.F,'jf',return_value=[]));s.enter_context(patch.object(a.F,'metadata_admission_blocked',return_value=False))
   s.enter_context(patch.object(a.F,'first_sweep_candidate',return_value=((1,KEY),True)))
   s.enter_context(patch('verification_remote.jobs',return_value=None))
   prepared=s.enter_context(patch.object(a,'prepared',side_effect=error))
   a.F.warm_availability(1)
   return prepared.call_count
 def busy_error(self):
  path=self.t.root/'busy.sqlite';first=sqlite3.connect(path);second=sqlite3.connect(path,timeout=0)
  try:
   first.execute('CREATE TABLE IF NOT EXISTS fixture(value TEXT)');first.commit();first.execute('BEGIN IMMEDIATE')
   try:second.execute("INSERT INTO fixture VALUES ('fixture')")
   except sqlite3.OperationalError as error:
    error.args=('private SQL https://private.invalid/credential-secret',)
    return error
   self.fail('Expected actual SQLite busy error')
  finally:first.rollback();second.close();first.close()
 def events(self,logger):
  return [json.loads(call.args[0].split('SQLite failure ',1)[1]) for call in logger.call_args_list if str(call.args[0]).startswith('SQLite failure ')]
 def test_worker_cleanup_preserves_case_siblings_other_cache_and_proof(self):
  base='prepared:'+KEY
  removed={base,base+':audio:0',base.upper()+':legacy',base+':עברית'}
  retained={base.upper(),base+'0','prepared:movie:tt0000002:audio:0','meta:movie:tt0000001','source-status:'+KEY}
  with a.db() as c:c.executemany('INSERT INTO cache VALUES (?,?,?)',[(k,'"fixture"',9999999999) for k in removed|retained])
  self.assertEqual(self.warm(a.SourceProviderWait('Fixture capacity',reason='capacity')),1)
  with a.F.db() as c:
   remaining={r[0] for r in c.execute('SELECT key FROM cache WHERE value=?',('"fixture"',))}
   self.assertEqual(remaining,retained)
   row=c.execute('SELECT state,checked,proof FROM validation WHERE key=?',(KEY,)).fetchone()
   self.assertEqual(row[:2],('pending',123));self.assertEqual(json.loads(row[2]),{'fixture':'preserved'})
   self.assertEqual(c.execute('SELECT count(*) FROM verification_claims').fetchone()[0],0)
 def test_actual_worker_cleanup_is_bounded_with_large_unrelated_cache(self):
  with a.db() as c:
   c.executemany('INSERT INTO cache VALUES (?,?,?)',((f'fixture:{n:08}','"unrelated"',9999999999) for n in range(100000)))
   c.execute('INSERT INTO cache VALUES (?,?,?)',('prepared:'+KEY+':audio:0','"target"',9999999999))
  original=sqlite3.connect;ticks=[0]
  def connect(*args,**kwargs):
   c=original(*args,**kwargs)
   def progress():ticks[0]+=1;return 0
   c.set_progress_handler(progress,100);return c
  with patch.object(a.sqlite3,'connect',side_effect=connect):
   self.assertEqual(self.warm(a.SourceProviderWait('Fixture capacity',reason='capacity')),1)
  self.assertLess(ticks[0]*100,100000,'Worker must not scan the unrelated cache')
  with a.db() as c:
   self.assertEqual(c.execute("SELECT count(*) FROM cache WHERE value='\"unrelated\"'").fetchone()[0],100000)
   self.assertIsNone(c.execute('SELECT key FROM cache WHERE key=?',('prepared:'+KEY+':audio:0',)).fetchone())
 def test_native_sqlite_details_are_safe_and_missing_fields_not_invented(self):
  error=self.busy_error();before=(error.args,error.sqlite_errorcode,error.sqlite_errorname)
  details=a.sqlite_failure_details(error,'worker attempt')
  self.assertEqual(details['sqlite_code'],sqlite3.SQLITE_BUSY);self.assertEqual(details['sqlite_name'],'SQLITE_BUSY')
  self.assertEqual((error.args,error.sqlite_errorcode,error.sqlite_errorname),before)
  self.assertNotIn('private.invalid',json.dumps(details));self.assertNotIn('credential-secret',json.dumps(details))
  generic=sqlite3.OperationalError('secret SQL');details=a.sqlite_failure_details(generic,'secret phase')
  self.assertEqual(details['stage'],'unknown');self.assertNotIn('sqlite_code',details);self.assertNotIn('sqlite_name',details)
  generic.sqlite_errorcode=5;generic.sqlite_errorname='SQLITE_CREDENTIAL_SECRET'
  self.assertNotIn('sqlite_name',a.sqlite_failure_details(generic,'preparation'))
  self.assertEqual(a.sqlite_failure_details(ValueError('secret'), 'preparation'),{})
  generic.sqlite_errorcode=sqlite3.SQLITE_BUSY_SNAPSHOT;generic.sqlite_errorname='SQLITE_BUSY_SNAPSHOT'
  self.assertEqual(a.sqlite_failure_details(generic,'preparation')['sqlite_name'],'SQLITE_BUSY_SNAPSHOT')
  generic.sqlite_errorcode=True
  self.assertNotIn('sqlite_code',a.sqlite_failure_details(generic,'preparation'))
 def test_preparation_logs_original_sqlite_error_before_existing_conversion(self):
  error=self.busy_error()
  with patch.object(a,'prepare_locked',side_effect=error),patch.object(a,'log') as logger:
   with self.assertRaises(a.Checkpoint) as caught:a.prepared(KEY,background=True)
  self.assertEqual(caught.exception.code,'coordinator_database');self.assertEqual(caught.exception.failure_category,'coordinator')
  events=self.events(logger);self.assertEqual(len(events),1);self.assertEqual(events[0]['stage'],'preparation')
  self.assertEqual(events[0]['sqlite_code'],5);self.assertEqual(events[0]['error_type'],'OperationalError')
  self.assertTrue(any(f['file']=='app.py' and f['function']=='prepared' for f in events[0]['frames']))
  self.assertLessEqual(len(events[0]['frames']),6)
  for frame in events[0]['frames']:self.assertNotIn('/',frame['file'])
  self.assertNotIn('private.invalid',json.dumps(events));self.assertNotIn('credential-secret',json.dumps(events))
 def test_outer_worker_logs_sqlite_failure_and_preserves_retry_claims_and_proof(self):
  error=self.busy_error()
  with patch.object(a,'log') as logger:self.assertEqual(self.warm(error),1)
  events=self.events(logger);self.assertEqual(len(events),1);self.assertEqual(events[0]['stage'],'worker attempt');self.assertEqual(events[0]['sqlite_code'],5)
  with a.F.db() as c:
   self.assertEqual(c.execute('SELECT outcome FROM verification_sweep_attempts WHERE key=?',(KEY,)).fetchone()[0],'queued')
   self.assertEqual(c.execute('SELECT count(*) FROM verification_claims').fetchone()[0],0)
   self.assertEqual(json.loads(c.execute('SELECT proof FROM validation WHERE key=?',(KEY,)).fetchone()[0]),{'fixture':'preserved'})
 def test_logger_failure_and_duplicate_reporting_do_not_change_exception(self):
  error=self.busy_error()
  with patch.object(a,'log',side_effect=OSError('private log failure')):
   a.log_sqlite_failure(error,'worker attempt')
  with patch.object(a,'log') as logger:
   a.log_sqlite_failure(error,'worker attempt');a.log_sqlite_failure(error,'preparation')
  self.assertEqual(len(self.events(logger)),1);self.assertEqual(error.sqlite_errorcode,5)
  with patch.object(a,'prepare_locked',side_effect=self.busy_error()),patch.object(a,'log',side_effect=OSError('fixture log failure')):
   # Existing playback-phase logging can also fail; isolate only the new
   # diagnostic hook when checking conversion parity.
   with patch.object(a,'playback_event'):
    with self.assertRaises(a.Checkpoint) as caught:a.prepared(KEY,background=True)
  self.assertEqual(caught.exception.code,'coordinator_database')
if __name__=='__main__':unittest.main()
