import json,sqlite3,tempfile,time,unittest
from pathlib import Path
import monitor
class RecoveryMetricsTests(unittest.TestCase):
 def test_recovery_counts_are_distinct_from_capacity_and_final_outcomes(self):
  with tempfile.TemporaryDirectory() as folder:
   root=Path(folder);data=root/'catalogue';data.mkdir();worker=root/'verification-worker';worker.mkdir();snapshot=root/'snapshot';snapshot.write_text(json.dumps({'summary':{'total':10,'v6':1},'time':time.time()}))
   with sqlite3.connect(worker/'verification-diagnostics.sqlite') as c:
    c.execute('CREATE TABLE receipts(scope TEXT,key TEXT,code TEXT,finished REAL)');c.execute('CREATE TABLE buckets(bucket TEXT,count INTEGER)')
    c.executemany('INSERT INTO receipts VALUES (?,?,?,?)',[('preparation','a','provider_recovery',time.time()),('preparation','a','provider_recovery',time.time()),('preparation','b','provider_recovery',time.time()),('preparation','c','provider_capacity',time.time()),('preparation','d','provider_recovery',time.time()-4000),('first_sweep','e','no_candidates',time.time())])
   rows=monitor.overview(data=data,snapshot=snapshot)
   found=next(r for r in rows if r['name']=='Provider recovery waits in last hour')
   self.assertEqual(found['value'],'2 titles / 3 events')
   self.assertIn('not failed media checks',found['detail'])
 def test_first_sweep_metric_excludes_scheduling_only_checkpoints(self):
  with tempfile.TemporaryDirectory() as folder:
   root=Path(folder);data=root/'catalogue';data.mkdir();worker=root/'verification-worker';worker.mkdir();snapshot=root/'snapshot';snapshot.write_text(json.dumps({'summary':{'total':10,'v6':1},'time':time.time()}))
   now=time.time()
   with sqlite3.connect(worker/'verification-diagnostics.sqlite') as c:
    c.execute('CREATE TABLE receipts(scope TEXT,key TEXT,code TEXT,finished REAL)');c.execute('CREATE TABLE buckets(bucket TEXT,count INTEGER)')
    c.executemany('INSERT INTO receipts VALUES (?,?,?,?)',[
     ('first_sweep','passed','none',now),
     ('first_sweep','empty','no_candidates',now),
     ('first_sweep','partial','partial_provider_coverage',now),
     ('first_sweep','busy_then_result','preparation_lock_busy',now),
     ('first_sweep','busy_then_result','source_too_slow',now),
     ('first_sweep','budget_only','preparation_budget',now),
     ('preparation','other_scope','preparation_lock_busy',now),
     ('first_sweep','old','no_candidates',now-4000),
    ])
   rows=monitor.overview(data=data,snapshot=snapshot)
   found=next(r for r in rows if r['name']=='First sweep outcomes in last hour')
   self.assertEqual(found['value'],'4')
   self.assertIn('excludes 1 titles',found['detail'])
   self.assertIn('preparation-lock',found['detail'])
if __name__=='__main__':unittest.main()
