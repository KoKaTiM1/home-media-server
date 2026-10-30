"""Bounded initial-sweep scheduling against a large persisted queue."""
import sqlite3
import tempfile
import time
import unittest
from pathlib import Path
from features import Features

class FirstSweepTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.path=Path(self.tmp.name)/'queue.sqlite'
        c=sqlite3.connect(self.path)
        c.executescript('''CREATE TABLE verification_sweeps(id INTEGER PRIMARY KEY,started REAL,rowid_highwater INTEGER,completed REAL);
        INSERT INTO verification_sweeps VALUES(1,1,100000,0);
        CREATE TABLE verification_sweep_attempts(sweep_id INTEGER,key TEXT,started REAL,finished REAL DEFAULT 0,outcome TEXT,priority INTEGER DEFAULT 0,source_version INTEGER DEFAULT 0,due REAL DEFAULT 0,PRIMARY KEY(sweep_id,key));
        CREATE INDEX verification_sweep_attempts_id ON verification_sweep_attempts(sweep_id,outcome);
        CREATE INDEX verification_sweep_order ON verification_sweep_attempts(sweep_id,outcome,priority DESC,source_version,due);
        CREATE INDEX verification_sweep_v5_order ON verification_sweep_attempts(sweep_id,priority DESC,due) WHERE outcome='queued' AND source_version=5;
        CREATE TABLE validation(key TEXT PRIMARY KEY,state TEXT,next_check REAL,proof TEXT,priority INTEGER);
        CREATE TABLE verification_claims(key TEXT PRIMARY KEY,expires REAL);
        CREATE TABLE verification_holds(key TEXT PRIMARY KEY,expires REAL);
        CREATE TABLE metadata_waits(key TEXT PRIMARY KEY,expires REAL);
        CREATE TABLE verification_audit(key TEXT PRIMARY KEY,reason TEXT,completed REAL);''')
        c.executemany("INSERT INTO validation VALUES(?,'pending',0,'{}',0)",((f'movie:tt{i:07}',) for i in range(100000)))
        c.executemany("INSERT INTO verification_sweep_attempts(sweep_id,key,started,outcome) VALUES(1,?,1,'queued')",((f'movie:tt{i:07}',) for i in range(100000)))
        c.commit();c.close()
        self.f=Features({});self.f.db=self.db
    def tearDown(self):self.tmp.cleanup()
    def db(self):
        c=sqlite3.connect(self.path)
        # A full queue scan exceeds this instruction budget; a bounded read fits.
        c.set_progress_handler(lambda:1,100000)
        return c
    def test_large_queue_is_bounded(self):
        self.assertEqual(self.f.first_sweep_candidate(),((1,'movie:tt0000000'),True))
    def test_claims_holds_and_metadata_waits_are_skipped(self):
        with sqlite3.connect(self.path) as c:
            for table,i in [('verification_claims',0),('verification_holds',1),('metadata_waits',2)]:
                c.execute(f'INSERT INTO {table} VALUES(?,?)',(f'movie:tt{i:07}',time.time()+600))
        self.assertEqual(self.f.first_sweep_candidate(),((1,'movie:tt0000003'),True))
    def test_existing_proof_is_reconciled_without_rechecking(self):
        with sqlite3.connect(self.path) as c:
            c.execute("INSERT INTO verification_audit VALUES('movie:tt0000000','speed-v6',1)")
        self.assertEqual(self.f.first_sweep_candidate(),((1,'movie:tt0000001'),True))
        with sqlite3.connect(self.path) as c:
            self.assertEqual(c.execute("SELECT outcome FROM verification_sweep_attempts WHERE key='movie:tt0000000'").fetchone()[0],'passed')
    def test_priority_order_reaches_prepared_titles_beyond_first_window(self):
        with sqlite3.connect(self.path) as c:
            c.execute("UPDATE verification_sweep_attempts SET priority=200 WHERE key='movie:tt0099999'")
            c.execute("UPDATE validation SET priority=200 WHERE key='movie:tt0099999'")
        self.assertEqual(self.f.first_sweep_candidate(),((1,'movie:tt0099999'),True))
    def test_series_parent_metadata_wait_is_respected(self):
        with sqlite3.connect(self.path) as c:
            for table in ('validation','verification_sweep_attempts'):
                c.execute(f"UPDATE {table} SET key='series:tt0000000:1:1' WHERE key='movie:tt0000000'")
            c.execute("INSERT INTO metadata_waits VALUES('series:tt0000000',?)",(time.time()+600,))
        self.assertEqual(self.f.first_sweep_candidate(),((1,'movie:tt0000001'),True))

    def test_v5_precedes_higher_priority_unverified_discovery(self):
        with sqlite3.connect(self.path) as c:
            c.execute("UPDATE verification_sweep_attempts SET source_version=5 WHERE key='movie:tt0099999'")
            c.execute("UPDATE validation SET state='verified',proof='{\"version\":5}' WHERE key='movie:tt0099999'")
            c.execute("UPDATE verification_sweep_attempts SET priority=999 WHERE key='movie:tt0000000'")
        self.assertEqual(self.f.first_sweep_candidate(),((1,'movie:tt0099999'),True))
        with sqlite3.connect(self.path) as c:
            c.execute("UPDATE verification_sweep_attempts SET outcome='deferred' WHERE key='movie:tt0099999'")
        self.assertEqual(self.f.first_sweep_candidate(),((1,'movie:tt0000000'),True))

    def test_held_v5_does_not_block_other_work(self):
        with sqlite3.connect(self.path) as c:
            c.execute("UPDATE verification_sweep_attempts SET source_version=5 WHERE key='movie:tt0099999'")
            c.execute("INSERT INTO verification_holds VALUES('movie:tt0099999',?)",(time.time()+600,))
        self.assertEqual(self.f.first_sweep_candidate(),((1,'movie:tt0000000'),True))

    def test_snapshot_already_exists_needs_no_write_lock(self):
        with sqlite3.connect(self.path) as c:
            c.execute('BEGIN IMMEDIATE')
            self.f.start_verification_sweep()

if __name__=='__main__':unittest.main()
