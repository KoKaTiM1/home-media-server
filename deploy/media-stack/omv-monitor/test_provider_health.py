import unittest,tempfile,sqlite3,json
from pathlib import Path
import monitor
class ProviderHealthTests(unittest.TestCase):
 def test_cooldown_empty_and_independent_source_are_distinct_and_private(self):
  with tempfile.TemporaryDirectory() as folder:
   p=Path(folder)
   with sqlite3.connect(p/'catalogue.sqlite') as c:
    c.execute('CREATE TABLE cache(key TEXT,value TEXT,expires REAL)')
    for key,v in {'source-provider-control':{'status':429,'cooldown':1200,'requests':7,'updated':1000},'mediafusion-provider-control':{'status':200,'updated':1000},'provider-observation:mediafusion':{'raw_rows':0,'provider_rows':0,'compatible':0},'provider-timing:mediafusion':{'updated':1000,'queue_wait_ms':2100,'http_ms':410},'provider-timing:jackett:yts':{'updated':1000,'queue_wait_ms':850,'result':'wait_capacity'},'jackett-yts-control':{'status':200,'updated':1000,'url':'secret-address','token':'private-token'}}.items():c.execute('INSERT INTO cache VALUES (?,?,99999)',(key,json.dumps(v)))
   rows=monitor.provider_health_rows(p,1001)
   self.assertEqual([r['value'] for r in rows],['Cooldown','Ready','Ready'])
   self.assertIn('0 raw provider rows',rows[1]['detail'])
   self.assertEqual(rows[2]['name'],'Jackett yts discovery')
   self.assertIn('slot wait 2.10s; HTTP 0.41s',rows[1]['detail'])
   self.assertIn('slot wait 0.85s; HTTP not started; wait_capacity',rows[2]['detail'])
   self.assertNotIn('private-token',str(rows));self.assertNotIn('secret-address',str(rows))
if __name__=='__main__':unittest.main()
