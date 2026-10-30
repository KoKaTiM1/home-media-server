"""Real SQLite writer contention must preserve exact ownership and time bounds."""
import sqlite3
import tempfile
import threading
import time
import types
import unittest
from pathlib import Path
from features import Features

class ClosingConnection(sqlite3.Connection):
    def __exit__(self,*args):
        try:return super().__exit__(*args)
        finally:self.close()

class ClaimRenewalTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.path=Path(self.tmp.name)/'state.sqlite'
        self.logs=[];self.budget=types.SimpleNamespace()
        self.f=Features({'db':self.db,'BUDGET':self.budget,'log':self.logs.append})
        with self.db() as c:
            c.execute('PRAGMA journal_mode=WAL')
            c.execute('CREATE TABLE verification_claims(key TEXT PRIMARY KEY,token TEXT,owner TEXT,expires REAL)')
            c.execute('INSERT INTO verification_claims VALUES(?,?,?,?)',('title','mine','windows',time.time()+30))
    def tearDown(self):self.tmp.cleanup()
    def db(self):return sqlite3.connect(self.path,timeout=.01,factory=ClosingConnection)
    def hold_writer(self,seconds):
        ready=threading.Event()
        def hold():
            with self.db() as c:
                c.execute('BEGIN IMMEDIATE');ready.set();time.sleep(seconds)
        t=threading.Thread(target=hold);t.start();ready.wait(2);return t
    def test_busy_writer_releases_then_renewal_succeeds(self):
        t=self.hold_writer(.65)
        try:self.assertTrue(self.f.renew_verification('title','mine'))
        finally:t.join()
        self.assertTrue(self.logs)
        with self.db() as c:self.assertGreater(c.execute('SELECT expires FROM verification_claims').fetchone()[0],time.time()+28)
    def test_wrong_owner_cannot_renew(self):
        self.assertFalse(self.f.renew_verification('title','other'))
    def test_expired_claim_is_not_resurrected(self):
        with self.db() as c:c.execute('UPDATE verification_claims SET expires=?',(time.time()-1,))
        self.assertFalse(self.f.renew_verification('title','mine'))
    def test_expiry_during_contention_cannot_be_extended(self):
        expiry=time.time()+.2
        with self.db() as c:c.execute('UPDATE verification_claims SET expires=?',(expiry,))
        t=self.hold_writer(.5)
        try:
            try:result=self.f.renew_verification('title','mine')
            except sqlite3.OperationalError:result=False
            self.assertFalse(result)
        finally:t.join()
        with self.db() as c:self.assertEqual(c.execute('SELECT expires FROM verification_claims').fetchone()[0],expiry)
    def test_job_deadline_bounds_wait(self):
        self.budget.deadline=time.monotonic()+.15
        t=self.hold_writer(.6);started=time.monotonic()
        try:
            with self.assertRaises(sqlite3.OperationalError):self.f.renew_verification('title','mine')
            self.assertLess(time.monotonic()-started,.35)
        finally:t.join()
    def test_non_lock_database_errors_are_not_retried(self):
        with self.db() as c:c.execute('DROP TABLE verification_claims')
        started=time.monotonic()
        with self.assertRaises(sqlite3.OperationalError):self.f.renew_verification('title','mine')
        self.assertLess(time.monotonic()-started,.3)

if __name__=='__main__':unittest.main()
