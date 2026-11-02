"""Expired cache maintenance must stay bounded and preserve durable state."""
import fcntl
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
import app


class CacheMaintenanceTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.data=Path(self.temp.name)
        self.patch=patch.object(app,'DATA',self.data)
        self.patch.start()
        with app.F.db() as conn:
            conn.execute("INSERT INTO cache VALUES ('expired-a','a',10)")
            conn.execute("INSERT INTO cache VALUES ('expired-b','b',20)")
            conn.execute("INSERT INTO cache VALUES ('live','c',200)")
            conn.execute("INSERT INTO cache VALUES ('no-expiry','d',NULL)")
            conn.execute("INSERT INTO validation(key,state,checked,proof) VALUES ('movie:tt1234567','verified',50,'proof')")
            conn.execute("INSERT INTO verification_sweeps(id,started,rowid_highwater,baseline_total) VALUES (1,1,1,1)")
            conn.execute("INSERT INTO verification_sweep_attempts(sweep_id,key,started,outcome) VALUES (1,'movie:tt1234567',1,'passed')")

    def tearDown(self):
        self.patch.stop()
        self.temp.cleanup()

    def test_expired_rows_are_deleted_in_bounded_batches_and_durable_state_is_untouched(self):
        self.assertEqual(app.prune_expired_cache(limit=1,batch_size=1,now=100),1)
        with app.F.db() as conn:
            self.assertEqual(conn.execute("SELECT key FROM cache WHERE key LIKE 'expired-%'").fetchall()[0][0],'expired-b')
            self.assertEqual(conn.execute("SELECT value FROM cache WHERE key='live'").fetchone()[0],'c')
            self.assertEqual(conn.execute("SELECT value FROM cache WHERE key='no-expiry'").fetchone()[0],'d')
            self.assertEqual(conn.execute("SELECT state,proof FROM validation WHERE key='movie:tt1234567'").fetchone(),('verified','proof'))
            self.assertEqual(conn.execute("SELECT outcome FROM verification_sweep_attempts WHERE key='movie:tt1234567'").fetchone()[0],'passed')
            self.assertIsNotNone(conn.execute("SELECT 1 FROM sqlite_master WHERE type='index' AND name='cache_expiry'").fetchone())
            plan=conn.execute('EXPLAIN QUERY PLAN SELECT rowid FROM cache WHERE expires<=? ORDER BY expires,rowid LIMIT 1',(100,)).fetchall()
            self.assertIn('cache_expiry',' '.join(row[3] for row in plan))

    def test_prune_refuses_unbounded_parameters(self):
        with self.assertRaises(ValueError):app.prune_expired_cache(limit=50001,now=100)
        with self.assertRaises(ValueError):app.prune_expired_cache(batch_size=1001,now=100)

    def test_scheduled_cache_pruner_is_separate_from_download_cleanup(self):
        units=Path(__file__).resolve().parents[1]/'..'/'systemd'
        service=(units/'media-catalogue-cache-prune.service').read_text()
        timer=(units/'media-catalogue-cache-prune.timer').read_text()
        self.assertIn('python /app/app.py cache-prune --limit 20000 --batch-size 500',service)
        self.assertIn('OnCalendar=*-*-* 04:30:00',timer)
        self.assertNotIn('cleanup.py',service)

    def test_concurrent_pruner_exits_without_mutation(self):
        path=self.data/'cache-prune.lock'
        with path.open('a') as lock:
            fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
            self.assertEqual(app.prune_expired_cache(now=100),0)
        with app.F.db() as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM cache WHERE expires<=100").fetchone()[0],2)


if __name__=='__main__':unittest.main()
