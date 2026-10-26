"""Durable metadata leases and receipts, separate from source verification."""
from contextlib import contextmanager
import hashlib
import json
from pathlib import Path
import re
import sqlite3
import time
import uuid

PROTOCOL = 'home-media-metadata/1'
TTL = 21600
LEASE_SECONDS = 180


class MetadataBusy(RuntimeError):
    pass


class InvalidSubmission(ValueError):
    pass


def identity(kind, imdb):
    if kind not in ('movie', 'series') or not isinstance(imdb, str) or not re.fullmatch(r'tt\d{5,12}', imdb):
        raise InvalidSubmission('Invalid metadata identity')
    return kind + ':' + imdb


class MetadataJobs:
    def __init__(self, path):
        self.path = Path(path)

    @contextmanager
    def db(self, write=False):
        conn = sqlite3.connect(self.path, timeout=3)
        conn.row_factory = sqlite3.Row
        try:
            if write:
                conn.execute('BEGIN IMMEDIATE')
            yield conn
            conn.commit()
        except BaseException:
            conn.rollback()
            raise
        finally:
            conn.close()

    def initialize(self, generation):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        # SQLite otherwise creates mode 0644 (0640 under the service umask).
        # Its WAL/SHM inherit the DB mode, so both service user and bridge group
        # need write access before either side creates those companion files.
        self.path.touch(mode=0o660, exist_ok=True)
        self.path.chmod(0o660)
        with self.db() as conn:
            conn.execute('PRAGMA journal_mode=WAL')
            conn.executescript('''
                CREATE TABLE IF NOT EXISTS control(key TEXT PRIMARY KEY,value TEXT);
                CREATE TABLE IF NOT EXISTS clients(name TEXT PRIMARY KEY,worker_id TEXT,seen REAL NOT NULL DEFAULT 0);
                CREATE TABLE IF NOT EXISTS jobs(
                  job_id TEXT PRIMARY KEY,kind TEXT NOT NULL,imdb TEXT NOT NULL,generation TEXT NOT NULL,
                  owner TEXT,worker_id TEXT,lease_id TEXT,expires REAL NOT NULL DEFAULT 0,
                  state TEXT NOT NULL DEFAULT 'pending',due REAL NOT NULL DEFAULT 0,priority INTEGER DEFAULT 0,
                  updated REAL NOT NULL DEFAULT 0);
                CREATE INDEX IF NOT EXISTS jobs_due ON jobs(kind,state,due,priority);
                CREATE TABLE IF NOT EXISTS receipts(
                  result_id TEXT PRIMARY KEY,job_id TEXT NOT NULL,lease_id TEXT NOT NULL,generation TEXT NOT NULL,
                  owner TEXT NOT NULL,worker_id TEXT NOT NULL,payload_hash TEXT NOT NULL,status TEXT NOT NULL,
                  fetched REAL NOT NULL,metadata TEXT,created REAL NOT NULL,applied TEXT NOT NULL DEFAULT 'pending');
                CREATE INDEX IF NOT EXISTS receipt_import ON receipts(applied,created);
            ''')
        with self.db(True) as conn:
            previous = self.value(conn, 'generation', '')
            if previous != generation:
                conn.execute("UPDATE jobs SET generation=?,state='pending',owner=NULL,worker_id=NULL,lease_id=NULL,expires=0", (generation,))
            self.put(conn, 'generation', generation)
            conn.execute("INSERT OR IGNORE INTO clients(name) VALUES ('windows')")

    @staticmethod
    def value(conn, key, default=0):
        row = conn.execute('SELECT value FROM control WHERE key=?', (key,)).fetchone()
        return json.loads(row[0]) if row else default

    @staticmethod
    def put(conn, key, value):
        conn.execute('INSERT OR REPLACE INTO control VALUES (?,?)', (key, json.dumps(value)))

    def set_enabled(self, enabled):
        with self.db(True) as conn:
            self.put(conn, 'enabled', bool(enabled))

    def enabled(self):
        with self.db() as conn:
            return self.value(conn, 'enabled', False)

    def seen(self):
        with self.db(True) as conn:
            conn.execute("UPDATE clients SET seen=? WHERE name='windows'", (time.time(),))

    def reserve_provider(self):
        now = time.time()
        with self.db(True) as conn:
            wait = max(self.value(conn, 'provider_after'), self.value(conn, 'provider_slot')) - now
            if wait > 0:
                return wait
            self.put(conn, 'provider_slot', now + 3)
            return 0

    def provider_backoff(self, status):
        if status in (429, 503):
            with self.db(True) as conn:
                self.put(conn, 'provider_after', max(self.value(conn, 'provider_after'), time.time() + (900 if status == 429 else 60)))

    def enqueue(self, entries):
        now = time.time()
        with self.db(True) as conn:
            generation = self.value(conn, 'generation')
            for kind, imdb, priority in entries:
                key = identity(kind, imdb)
                conn.execute('INSERT INTO jobs(job_id,kind,imdb,generation,priority,updated) VALUES (?,?,?,?,?,?) '
                             'ON CONFLICT(job_id) DO UPDATE SET priority=MAX(priority,excluded.priority)',
                             (key, kind, imdb, generation, int(priority), now))

    @staticmethod
    def task(row):
        return {field: row[field] for field in ('job_id', 'lease_id', 'generation', 'kind', 'imdb')}

    def bind(self, conn, worker_id):
        if not isinstance(worker_id, str) or not re.fullmatch(r'[A-Za-z0-9_.:-]{1,128}', worker_id):
            raise InvalidSubmission('Invalid worker identity')
        row = conn.execute("SELECT worker_id FROM clients WHERE name='windows'").fetchone()
        if row and row[0] not in (None, worker_id):
            raise InvalidSubmission('Token is bound to another worker')
        conn.execute("UPDATE clients SET worker_id=?,seen=? WHERE name='windows'", (worker_id, time.time()))

    def claim(self, owner, worker_id, kind, imdb=None):
        now = time.time()
        with self.db(True) as conn:
            if not self.value(conn, 'enabled', False):
                raise MetadataBusy('Metadata coordinator is not enabled')
            if owner == 'windows':
                if kind != 'series':
                    raise InvalidSubmission('Token permits series metadata only')
                self.bind(conn, worker_id)
            if max(self.value(conn, 'provider_after'), self.value(conn, 'provider_slot')) > now:
                return None
            conn.execute("UPDATE jobs SET state='pending',owner=NULL,worker_id=NULL,lease_id=NULL,expires=0 WHERE state='leased' AND expires<=?", (now,))
            # One outstanding job per worker; resuming a lost claim response
            # returns its exact identity rather than allocating more work.
            row = conn.execute("SELECT * FROM jobs WHERE state='leased' AND owner=? AND worker_id=? AND expires>?", (owner, worker_id, now)).fetchone()
            if row:
                if owner == 'server' and (row['kind'] != kind or row['imdb'] != imdb):
                    return None
                return self.task(row)
            if owner == 'server' and kind == 'series':
                remote = conn.execute("SELECT seen FROM clients WHERE name='windows'").fetchone()
                if remote and remote[0] > now - 120:
                    return None
            query = "SELECT * FROM jobs WHERE kind=? AND due<=? AND state IN ('pending','retry','done')"
            params = [kind, now]
            if imdb is not None:
                query += ' AND imdb=?'; params.append(imdb)
            row = conn.execute(query + ' ORDER BY priority DESC,due,updated LIMIT 1', params).fetchone()
            if not row:
                return None
            lease = uuid.uuid4().hex
            conn.execute("UPDATE jobs SET state='leased',owner=?,worker_id=?,lease_id=?,expires=?,updated=? WHERE job_id=?",
                         (owner, worker_id, lease, now + LEASE_SECONDS, now, row['job_id']))
            self.put(conn, 'provider_slot', now + 3)
            return self.task(conn.execute('SELECT * FROM jobs WHERE job_id=?', (row['job_id'],)).fetchone())

    def renew(self, owner, worker_id, task):
        now = time.time()
        with self.db(True) as conn:
            if owner == 'windows':
                self.bind(conn, worker_id)
            row = self.owned(conn, owner, worker_id, task, now)
            if row:
                conn.execute('UPDATE jobs SET expires=? WHERE job_id=?', (now + LEASE_SECONDS, row['job_id']))
            return bool(row)

    def owned(self, conn, owner, worker_id, task, now):
        if not self.value(conn, 'enabled', False) or task.get('generation') != self.value(conn, 'generation'):
            return None
        return conn.execute("SELECT * FROM jobs WHERE job_id=? AND lease_id=? AND generation=? AND kind=? AND imdb=? "
                            "AND owner=? AND worker_id=? AND state='leased' AND expires>?",
                            (task.get('job_id'), task.get('lease_id'), task.get('generation'), task.get('kind'),
                             task.get('imdb'), owner, worker_id, now)).fetchone()

    def receive(self, owner, worker_id, task, result):
        now = time.time()
        if not isinstance(result, dict) or not re.fullmatch(r'[a-f0-9]{32}', str(result.get('result_id', ''))):
            raise InvalidSubmission('Invalid result identity')
        encoded = json.dumps({'task': task, 'result': result}, sort_keys=True, ensure_ascii=False, allow_nan=False, separators=(',', ':'))
        digest = hashlib.sha256(encoded.encode()).hexdigest()
        with self.db(True) as conn:
            if owner == 'windows':
                self.bind(conn, worker_id)
            old = conn.execute('SELECT * FROM receipts WHERE result_id=?', (result['result_id'],)).fetchone()
            if old:
                if old['owner'] != owner or old['worker_id'] != worker_id or old['payload_hash'] != digest:
                    raise InvalidSubmission('Result identity reused with different ownership or data')
                return 'duplicate'
            row = self.owned(conn, owner, worker_id, task, now)
            if not row:
                return 'stale'
            fetched = result.get('fetched_at')
            if (type(fetched) not in (float, int) or not now - 3600 <= fetched <= now + 60
                    or result.get('source') != 'cinemeta' or result.get('status') not in ('ok', 'retry', 'not_found')):
                raise InvalidSubmission('Invalid metadata result')
            meta = result.get('metadata')
            if result['status'] == 'ok':
                if (not isinstance(meta, dict) or meta.get('id') != row['imdb'] or meta.get('type') != row['kind']
                        or not isinstance(meta.get('name'), str) or not meta['name'].strip()):
                    raise InvalidSubmission('Result metadata identity mismatch')
                due, state = fetched + TTL, 'done'
            else:
                meta = None
                due, state = now + (86400 if result['status'] == 'not_found' else 300), 'retry'
            status = result.get('provider_status')
            if status == 429 or status == 503:
                delay = result.get('retry_after_seconds', 60)
                if type(delay) is not int:
                    raise InvalidSubmission('Invalid cooldown')
                self.put(conn, 'provider_after', max(self.value(conn, 'provider_after'), now + max(1, min(delay, 3600))))
            # Receipt and immutable import handoff are one durable transaction.
            conn.execute('INSERT INTO receipts VALUES (?,?,?,?,?,?,?,?,?,?,?,?)',
                         (result['result_id'], row['job_id'], row['lease_id'], row['generation'], owner, worker_id,
                          digest, result['status'], fetched, json.dumps(meta, ensure_ascii=False) if meta else None,
                          now, 'pending' if meta else 'not_applicable',))
            conn.execute('UPDATE jobs SET state=?,due=?,expires=0,updated=? WHERE job_id=?', (state, due, now, row['job_id']))
            return 'accepted'

    def release(self, task, status=None):
        with self.db(True) as conn:
            row = self.owned(conn, 'server', 'server', task, time.time())
            if row:
                conn.execute("UPDATE jobs SET state='retry',expires=0,due=? WHERE job_id=?", (time.time() + 30, row['job_id']))
            if status in (429, 503):
                self.put(conn, 'provider_after', max(self.value(conn, 'provider_after'), time.time() + (900 if status == 429 else 60)))

    def pending_imports(self, limit=20, key=None):
        with self.db() as conn:
            sql = "SELECT * FROM receipts WHERE applied='pending'"
            params = []
            if key is not None:
                sql += ' AND job_id=?'; params.append(key)
            return [dict(r) for r in conn.execute(sql + ' ORDER BY created LIMIT ?', (*params, limit))]

    def imported(self, result_id, outcome):
        if outcome not in ('applied', 'kept_newer', 'rejected'):
            raise InvalidSubmission('Invalid import outcome')
        with self.db(True) as conn:
            conn.execute("UPDATE receipts SET applied=?,metadata=NULL WHERE result_id=? AND applied='pending'", (outcome, result_id))

    def status(self):
        with self.db() as conn:
            return {'jobs': {r[0]: r[1] for r in conn.execute('SELECT state,COUNT(*) FROM jobs GROUP BY state')},
                    'imports': {r[0]: r[1] for r in conn.execute('SELECT applied,COUNT(*) FROM receipts GROUP BY applied')},
                    'windows_imports': {r[0]: r[1] for r in conn.execute("SELECT applied,COUNT(*) FROM receipts WHERE owner='windows' GROUP BY applied")},
                    'server_imports': {r[0]: r[1] for r in conn.execute("SELECT applied,COUNT(*) FROM receipts WHERE owner='server' GROUP BY applied")},
                    'windows_seen': conn.execute("SELECT seen FROM clients WHERE name='windows'").fetchone()[0],
                    'provider_after': self.value(conn, 'provider_after'),
                    'enabled': self.value(conn, 'enabled', False)}

    def generation(self):
        with self.db() as conn:
            return self.value(conn, 'generation')

    def seed(self, catalogue):
        """Called by the trusted importer, not the network-facing receiver."""
        now = time.time()
        with sqlite3.connect(Path(catalogue).as_uri() + '?mode=ro', uri=True, timeout=2) as conn:
            rows = conn.execute("SELECT v.key FROM verification_audit a JOIN validation v ON v.key=a.key "
                                "LEFT JOIN cache c ON c.key='meta:series:'||substr(v.key,8,instr(substr(v.key,8),':')-1) "
                                "WHERE a.completed=0 AND v.key LIKE 'series:%' "
                                "AND (v.state='verified' OR v.next_check<=?) "
                                "AND NOT EXISTS(SELECT 1 FROM verification_holds h WHERE h.key=v.key AND h.expires>?) "
                                "AND (c.expires IS NULL OR c.expires<=?) "
                                "ORDER BY a.priority DESC,a.scheduled LIMIT 100", (now, now, now+900)).fetchall()
            initial_pending=conn.execute('SELECT 1 FROM verification_audit WHERE completed=0 LIMIT 1').fetchone()
            if not rows and not initial_pending:
                rows = conn.execute("SELECT key FROM validation INDEXED BY validation_work WHERE state!='verified' "
                                    "AND next_check<=? AND key LIKE 'series:%:1:1' "
                                    "AND NOT EXISTS(SELECT 1 FROM verification_holds h WHERE h.key=validation.key AND h.expires>?) "
                                    "ORDER BY priority DESC,next_check LIMIT 100", (now, now)).fetchall()
            entries = []
            for position, imdb in enumerate(dict.fromkeys(row[0].split(':')[1] for row in rows)):
                cached = conn.execute('SELECT expires FROM cache WHERE key=?', ('meta:series:' + imdb,)).fetchone()
                if not cached or cached[0] <= now + 900:
                    entries.append(('series', imdb, 1000 - position))
        self.enqueue(entries)
        return len(entries)


def from_marker(data):
    marker = Path(data) / 'metadata-helper.enabled'
    if not marker.is_file():
        return None
    config = json.loads(marker.read_text())
    jobs = MetadataJobs(config['queue_path'])
    return jobs if jobs.enabled() else None
