import unittest,tempfile,sqlite3,time
from pathlib import Path
from features import Features
class Conn(sqlite3.Connection):
 def __exit__(self,*args):
  try:return super().__exit__(*args)
  finally:self.close()
class SeedTests(unittest.TestCase):
 def test_already_seeded_titles_need_no_writer_lock(self):
  with tempfile.TemporaryDirectory() as t:
   path=Path(t)/'catalogue.sqlite'
   def db():return sqlite3.connect(path,timeout=.15,factory=Conn)
   with db() as c:
    c.execute('PRAGMA journal_mode=WAL')
    c.executescript("CREATE TABLE cache(key TEXT PRIMARY KEY,value TEXT,expires REAL);CREATE TABLE validation(key TEXT PRIMARY KEY,priority INTEGER,state TEXT DEFAULT 'pending',proof TEXT,next_check REAL DEFAULT 0);CREATE TABLE discovery(kind TEXT,imdb TEXT);CREATE TABLE items(kind TEXT,imdb TEXT);INSERT INTO cache VALUES('validation-migrated-v2','true',9999999999);INSERT INTO discovery VALUES('movie','tt0000001');INSERT INTO validation VALUES('movie:tt0000001',150,'verified','proof-fixture',12345);")
   f=Features({'DATA':Path(t)});f.db=db
   writer=db();writer.execute('BEGIN IMMEDIATE')
   try:
    started=time.monotonic();f.seed_validation();self.assertLess(time.monotonic()-started,.1)
   finally:writer.rollback();writer.close()
   with db() as c:
    self.assertEqual(c.execute('SELECT priority,state,proof,next_check FROM validation').fetchone(),(150,'verified','proof-fixture',12345))
    c.executescript("INSERT INTO discovery VALUES('series','tt0000002');INSERT INTO items VALUES('series','tt0000002');INSERT INTO items VALUES('movie','tt0000003');")
   f.seed_validation()
   with db() as c:
    self.assertEqual(c.execute('SELECT key,priority FROM validation ORDER BY key').fetchall(),[('movie:tt0000001',150),('movie:tt0000003',20),('series:tt0000002:1:1',20)])
    self.assertEqual(c.execute("SELECT state,proof,next_check FROM validation WHERE key='movie:tt0000001'").fetchone(),('verified','proof-fixture',12345))

import test_features as fixtures
class RealSchemaSeedTests(unittest.TestCase):
 setUp=fixtures.FeatureTests.setUp
 def test_initialized_production_schema_can_seed_existing_rows_while_writer_is_held(self):
  a=fixtures.a
  with a.F.db() as c:
   c.execute("INSERT INTO discovery VALUES ('movie','tt0000001','{}')")
   c.execute("INSERT INTO validation(key,priority,state,proof,next_check) VALUES ('movie:tt0000001',150,'verified','{}',12345)")
  a.remember('validation-migrated-v2',True,3600)
  writer=a.db();writer.execute('BEGIN IMMEDIATE')
  prior=getattr(a.BUDGET,'deadline',None);a.BUDGET.deadline=time.monotonic()+.5
  try:
   started=time.monotonic();a.F.seed_validation();self.assertLess(time.monotonic()-started,.2)
  finally:
   a.BUDGET.deadline=prior;writer.rollback();writer.close()

if __name__=='__main__':unittest.main()
