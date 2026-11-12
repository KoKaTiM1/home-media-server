"""Isolated discovery trial regressions; no external HTTP or production writes."""
import io,json,sqlite3,tempfile,unittest
from pathlib import Path
import comet_trial as c
CAPS=b'<caps><searching><movie-search available="yes" supportedParams="q,imdbid"/><tv-search available="yes" supportedParams="q,imdbid,season,ep"/></searching></caps>'
APP=Path(__file__).with_name('app.py')
def row(n,kind='movie'):
 key=f'{kind}:tt{n:07d}'+(':1:1' if kind=='series' else '')
 return {'key':key,'metadata':{'name':'Example','names':['Example'],'year':'2020','votes':n%3*80,'meta':{'name':'Example','year':2020}},'baseline':{'all_retained_hashes':[],'fresh_hashes':[],'observations':[]}}
def listing(key,title=None):
 title=title or ('Example.2020.'+('S01E01.' if key.startswith('series:') else '')+'1080p.x264.AAC')
 return f'<rss xmlns:t="http://torznab.com/schemas/2015/feed"><channel><item><title>{title}</title><t:attr name="imdbid" value="{key.split(":")[1]}"/><t:attr name="infohash" value="{"a"*40}"/></item></channel></rss>'.encode()
class TrialTests(unittest.TestCase):
 def setUp(self):
  self.temp=tempfile.TemporaryDirectory();self.out=Path(self.temp.name)
  self.filter,self.sha=c.load_filter(APP)
  self.sample=c.select_sample([row(n,k) for k in ('movie','series') for n in range(100,170)],[row(n,k) for k in ('movie','series') for n in range(1,30)])
  c.save(self.out/'sample.json',{'endpoint':c.ENDPOINT,'filter_sha':self.sha,'sample':self.sample})
 def tearDown(self):self.temp.cleanup()
 def run_trial(self,response):
  starts=[];now=[1000.]
  def transport(q):
   starts.append(now[0]);return {'status':200,'body':CAPS} if q['t']=='caps' else response(q)
  report=c.run(self.out,transport=transport,clock=lambda:now[0],sleep=lambda s:now.__setitem__(0,now[0]+s),filter_path=APP)
  return report,starts
 def test_sample_counts_reproducible_and_distinct(self):
  self.assertEqual(len(self.sample),100);self.assertEqual(sum(r['control'] for r in self.sample),20)
  self.assertEqual(len({':'.join(r['key'].split(':')[:2]) for r in self.sample}),100)
  self.assertEqual({r['vote_band'] for r in self.sample},{'zero','low','higher'})
  with self.assertRaises(ValueError):c.select_sample([],[])
 def test_identity_year_episode_and_codec(self):
  sample=row(1)
  self.assertEqual(c.analyze(listing(sample['key']),sample,self.filter)['identity_matched'],1)
  for title in ('Example.2019.1080p.x264','Example.2020.2160p.x264','Example.2020.1080p.AV1','Example.2020.1080p.x264.iso'):
   self.assertEqual(c.analyze(listing(sample['key'],title),sample,self.filter)['identity_matched'],0)
  self.assertEqual(c.analyze(listing('movie:tt0000002'),sample,self.filter)['identity_matched'],0)
  series=row(1,'series')
  self.assertEqual(c.analyze(listing(series['key']),series,self.filter)['identity_matched'],1)
  self.assertEqual(c.analyze(listing(series['key'],'Example.Spinoff.S01E01.1080p.x264'),series,self.filter)['identity_matched'],0)
 def test_malformed_and_http_only(self):
  for raw in (b'<html/>',b'<!DOCTYPE rss><rss/>',b'broken'):
   with self.assertRaises(Exception):c.analyze(raw,row(1),self.filter)
  raw=listing(row(1)['key']).replace(b'<t:attr name="infohash" value="'+b'a'*40+b'"/>',b'<link>https://example.invalid/file</link>')
  self.assertEqual(c.analyze(raw,row(1),self.filter)['identity_matched'],0)
 def test_base32_and_duplicate_hashes(self):
  import base64
  raw=listing(row(1)['key']);encoded=base64.b32encode(bytes.fromhex('a'*40))
  converted=raw.replace(b'a'*40,encoded)
  self.assertEqual(c.analyze(converted,row(1),self.filter)['hashes'],['a'*40])
  duplicate=raw.replace(b'</channel>',raw.split(b'<channel>')[1].split(b'</channel>')[0]+b'</channel>')
  result=c.analyze(duplicate,row(1),self.filter)
  self.assertEqual(result['normalized'],2);self.assertEqual(result['identity_matched'],1)
 def test_prepare_excludes_holds_and_manual_rows_without_writes(self):
  from unittest.mock import patch
  catalogue=self.out/'catalogue';index=self.out/'index';diagnostics=self.out/'diagnostics'
  conn=sqlite3.connect(catalogue)
  conn.executescript('CREATE TABLE cache(key,value,expires);CREATE TABLE validation(key,state,checked,proof);CREATE TABLE verification_holds(key,expires,reason);CREATE TABLE manual_rechecks(key,status);CREATE TABLE items(key,meta);CREATE TABLE home_titles(kind,imdb,name,original);CREATE TABLE aliases(imdb,english);')
  idx=sqlite3.connect(index);idx.execute('CREATE TABLE titles(kind,imdb,name,original,year,votes)')
  diag=sqlite3.connect(diagnostics);diag.execute('CREATE TABLE receipts(key,code,finished)')
  for kind in ('movie','series'):
   for n in range(1,100):
    r=row(n,kind);key=r['key'];imdb=key.split(':')[1]
    idx.execute('INSERT INTO titles VALUES (?,?,?,?,?,?)',(kind,imdb,'Example','Example',2020,n))
    conn.execute('INSERT INTO validation VALUES (?,?,?,?)',(key,'verified' if n<30 else 'pending',n,json.dumps({'version':6})))
    if n>=30:diag.execute('INSERT INTO receipts VALUES (?,?,?)',(key,'no_candidates',n))
  conn.execute('INSERT INTO verification_holds VALUES (?,?,?)',('series:tt0000001',9999999999,'private hold'))
  conn.execute('INSERT INTO manual_rechecks VALUES (?,?)',('movie:tt0000001','pending'))
  for db in (conn,idx,diag):db.commit();db.close()
  config=self.out/'config';config.write_text('{}');before={p:p.read_bytes() for p in (catalogue,index,diagnostics,config)}
  with patch.object(c,'load_filter',return_value=(self.filter,self.sha)):
   data=c.prepare(catalogue,index,diagnostics,config,self.out/'prepared')
  self.assertEqual(len(data['sample']),100)
  self.assertFalse(any(r['key'] in ('movie:tt0000001','series:tt0000001:1:1') for r in data['sample']))
  for p,content in before.items():self.assertEqual(p.read_bytes(),content)
 def test_baseline_readonly_and_freshness(self):
  db=self.out/'db';conn=sqlite3.connect(db)
  conn.executescript('CREATE TABLE cache(key,value,expires);CREATE TABLE validation(key,proof);')
  conn.execute('INSERT INTO cache VALUES (?,?,?)',('streams:movie:tt0000001',json.dumps([{'infoHash':'a'*40,'url':'secret'}]),200))
  conn.execute('INSERT INTO cache VALUES (?,?,?)',('prepared:movie:tt0000001',json.dumps({'hash':'b'*40}),50));conn.commit();conn.close()
  conn=c.readonly(db);base=c.baseline(conn,'movie:tt0000001',{},100)
  self.assertEqual(base['fresh_hashes'],['a'*40]);self.assertEqual(len(base['all_retained_hashes']),2)
  self.assertNotIn('secret',json.dumps(base))
  with self.assertRaises(sqlite3.OperationalError):conn.execute('DELETE FROM cache')
  conn.close()
 def test_empty_controls_stop_before_negative_requests(self):
  report,starts=self.run_trial(lambda q:{'status':200,'body':b'<rss><channel/></rss>'})
  self.assertEqual(report['queried_keys'],20);self.assertEqual(len(starts),21)
  self.assertTrue(all(b-a>=15 for a,b in zip(starts,starts[1:])))
 def test_rate_limit_stops_without_retry(self):
  for status in (403,429):
   with self.subTest(status=status):
    (self.out/'report.json').unlink(missing_ok=True);(self.out/'state.json').unlink(missing_ok=True);(self.out/'results.json').unlink(missing_ok=True);(self.out/'caps.xml').unlink(missing_ok=True)
    report,starts=self.run_trial(lambda q:{'status':status,'retry_after':600})
    self.assertEqual(report['queried_keys'],1);self.assertEqual(len(starts),2)
 def test_three_errors_stop(self):
  report,starts=self.run_trial(lambda q:{'error':'timeout'})
  self.assertEqual(report['queried_keys'],3);self.assertEqual(len(starts),4)
 def test_complete_controls_gate_and_no_proof(self):
  def response(q):
   key=('movie:' if q['t']=='movie' else 'series:')+q['imdbid']+(':'+q['season']+':'+q['ep'] if q['t']=='tvsearch' else '')
   return {'status':200,'body':listing(key)}
  report,starts=self.run_trial(response)
  self.assertEqual(report['queried_keys'],100);self.assertEqual(len(starts),101)
  self.assertEqual(report['verification_proofs_created'],0)
  self.assertEqual(c.run(self.out,transport=lambda q:self.fail('repeated request'),filter_path=APP),report)
 def test_interrupted_request_not_retried(self):
  c.save(self.out/'state.json',{'requests':2,'next_request':1000,'inflight':True})
  report=c.run(self.out,transport=lambda q:self.fail('repeated request'),filter_path=APP)
  self.assertEqual(report['reason'],'interrupted_request_not_retried')
 def test_response_limit_and_deadline(self):
  class Response:
   def __init__(self,size):self.data=io.BytesIO(b'x'*size)
   def read1(self,n):return self.data.read(n)
  with self.assertRaises(ValueError):c.read_limited(Response(c.MAX_BYTES+1),100,clock=lambda:1)
  with self.assertRaises(TimeoutError):c.read_limited(Response(1),1,clock=lambda:2)
if __name__=='__main__':unittest.main()
