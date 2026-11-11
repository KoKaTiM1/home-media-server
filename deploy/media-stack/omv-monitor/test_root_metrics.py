"""Root cause is displayed separately from operational checkpoint counts."""
import json,sqlite3,tempfile,time,unittest
from pathlib import Path
import monitor
class RootMetricTests(unittest.TestCase):
 def test_root_and_legacy_buckets_are_separate(self):
  with tempfile.TemporaryDirectory() as folder:
   root=Path(folder);data=root/'catalogue';data.mkdir();worker=root/'verification-worker';worker.mkdir();snapshot=root/'snapshot';snapshot.write_text(json.dumps({'summary':{'total':10,'v6':1},'time':time.time()}))
   with sqlite3.connect(worker/'verification-diagnostics.sqlite') as c:
    c.execute('CREATE TABLE receipts(scope TEXT,key TEXT,code TEXT,finished REAL)');c.execute('CREATE TABLE buckets(bucket TEXT,count INTEGER)')
    c.executemany('INSERT INTO buckets VALUES (?,?)',[('worker_job/checkpoint/transport/unclassified',3),('root/worker_job/speed_rejected/speed_read_timeout',2)])
   rows=monitor.overview(data=data,snapshot=snapshot)
   legacy=[r for r in rows if r['name'].startswith('Latest title outcome:')];cause=[r for r in rows if r['name'].startswith('Reported root cause:')]
   self.assertEqual(len(legacy),1);self.assertIn('unclassified',legacy[0]['name']);self.assertEqual(legacy[0]['value'],'3')
   self.assertEqual(len(cause),1);self.assertIn('speed_read_timeout',cause[0]['name']);self.assertEqual(cause[0]['value'],'2');self.assertIn('Diagnostic-only',cause[0]['detail'])
if __name__=='__main__':unittest.main()
