"""Metadata-only LAN helper. Never opens the server/Jellyfin databases or torrents."""
import argparse
import contextlib
import fcntl
import hashlib
import hmac
import http.client
import ipaddress
import json
import logging
from logging.handlers import RotatingFileHandler
import math
import os
from pathlib import Path
import re
import signal
import sqlite3
import ssl
import threading
import time
import urllib.parse
import uuid

PROTOCOL = "home-media-metadata/1"
VERSION = "0.1.0"
PREFIX = "/v1/metadata-worker"
PROVIDER = "v3-cinemeta.strem.io"
PROVIDER_HOSTS = {PROVIDER, "cinemeta-live.strem.io"}
MAX_META = 4 * 1024 * 1024
MAX_REPLY = 256 * 1024
LOG = logging.getLogger("metadata-worker")
STOP = threading.Event()


class SafeError(Exception):
    """Only constant, non-secret messages may be logged."""


class RemoteError(SafeError):
    def __init__(self, status, retry_after=60):
        self.status = status
        self.retry_after = max(1, min(3600, retry_after))
        super().__init__(f"Remote HTTP {status}")


def bounded_json(response, limit):
    raw = response.read(limit + 1)
    if len(raw) > limit:
        raise SafeError("Reply exceeds size limit")
    try:
        value = json.loads(raw)
    except (ValueError, UnicodeError):
        raise SafeError("Reply is not valid JSON") from None
    if not isinstance(value, dict):
        raise SafeError("Reply must be a JSON object")
    return value


def retry_seconds(response):
    # Numeric Retry-After only; do not trust arbitrary header text in logs.
    value = response.getheader("Retry-After", "60")
    return max(1, min(3600, int(value))) if value.isdigit() else 60


def metadata_identity(meta, kind, imdb):
    if not isinstance(meta, dict):
        raise SafeError("Provider metadata is missing")
    if meta.get("id") != imdb or meta.get("type") != kind:
        raise SafeError("Provider metadata identity does not match assignment")
    if meta.get("imdb_id", imdb) != imdb:
        raise SafeError("Provider IMDb identity conflicts with assignment")
    if not isinstance(meta.get("name"), str) or not meta["name"].strip():
        raise SafeError("Provider metadata has no title")
    if len(meta["name"]) > 1000:
        raise SafeError("Provider title exceeds size limit")
    # Only metadata fields the current server understands. Ignore unknown URLs,
    # stream manifests, executable markup and addon transport information.
    allowed = ("id", "imdb_id", "type", "name", "originalName", "description",
               "releaseInfo", "released", "year", "genres", "runtime", "imdbRating",
               "poster", "background", "logo", "director", "cast", "country")
    cleaned = {}
    for key in allowed:
        value = meta.get(key)
        if value is None:
            continue
        if key in ("poster", "background", "logo"):
            if not isinstance(value, str) or len(value) > 4096:
                continue
            parsed = urllib.parse.urlsplit(value)
            if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
                continue
        elif isinstance(value, str):
            if len(value) > 65536:
                raise SafeError("Provider text exceeds size limit")
        elif isinstance(value, list):
            if len(value) > 200 or not all(isinstance(x, str) and len(x) <= 1000 for x in value):
                continue
        elif isinstance(value, float) and not math.isfinite(value):
            continue
        elif not isinstance(value, (int, float)):
            continue
        cleaned[key] = value
    if kind == "series":
        videos = meta.get("videos", [])
        if not isinstance(videos, list) or len(videos) > 20000:
            raise SafeError("Provider episode list is invalid")
        episodes = []
        seen = set()
        for item in videos:
            if not isinstance(item, dict):
                continue
            season, episode = item.get("season"), item.get("episode")
            if type(season) is not int or type(episode) is not int:
                continue
            if not 0 <= season <= 300 or not 1 <= episode <= 2000 or (season, episode) in seen:
                continue
            expected = f"{imdb}:{season}:{episode}"
            if item.get("id", expected) != expected:
                continue
            out = {"id": expected, "season": season, "episode": episode}
            for key in ("name", "title", "released", "overview", "thumbnail"):
                value = item.get(key)
                if isinstance(value, str) and len(value) <= 65536:
                    if key == "thumbnail" and not value.startswith("https://"):
                        continue
                    out[key] = value
            seen.add((season, episode))
            episodes.append(out)
        cleaned["videos"] = episodes
    if len(json.dumps(cleaned, ensure_ascii=False).encode()) > MAX_META:
        raise SafeError("Cleaned metadata exceeds size limit")
    return cleaned


def provider_redirect(host, path, location):
    target = urllib.parse.urlsplit(urllib.parse.urljoin("https://" + host + path, location))
    if (target.scheme != "https" or target.hostname not in PROVIDER_HOSTS or target.port not in (None, 443)
            or target.username or target.password or target.query or target.fragment or target.path != path):
        raise SafeError("Provider redirect is outside the permitted metadata service")
    return target.hostname


def fetch_metadata(kind, imdb):
    if kind not in ("movie", "series") or not re.fullmatch(r"tt\d{5,12}", imdb):
        raise SafeError("Invalid provider assignment")
    # Cinemeta currently redirects to its own live service. Permit at most two
    # HTTPS redirects between these explicit hosts with the exact same ID path.
    # Coordinator-supplied URLs/commands and redirects to arbitrary hosts fail.
    host = PROVIDER
    path = f"/meta/{kind}/{imdb}.json"
    for hop in range(3):
        conn = http.client.HTTPSConnection(host, timeout=15, context=ssl.create_default_context())
        try:
            conn.request("GET", path,
                         headers={"User-Agent": f"HomeMediaMetadataWorker/{VERSION}", "Accept": "application/json"})
            response = conn.getresponse()
            if response.status in (301, 302, 303, 307, 308):
                if hop == 2:
                    raise SafeError("Provider redirect limit reached")
                host = provider_redirect(host, path, response.getheader("Location", ""))
                continue
            if response.status != 200:
                raise RemoteError(response.status, retry_seconds(response))
            return metadata_identity(bounded_json(response, MAX_META).get("meta"), kind, imdb)
        finally:
            conn.close()


class ControlAPI:
    def __init__(self, config, *, loopback_test=False):
        url = urllib.parse.urlsplit(config.get("server_url", ""))
        if not url.hostname or url.username or url.password or url.query or url.fragment or url.path not in ("", "/"):
            raise SafeError("Set server_url to the configured coordinator HTTPS origin")
        if url.scheme != "https":
            if not (loopback_test and url.scheme == "http" and ipaddress.ip_address(url.hostname).is_loopback):
                raise SafeError("Coordinator requires HTTPS")
        token = config.get("token", "")
        if not isinstance(token, str) or not re.fullmatch(r"[A-Za-z0-9_-]{32,256}", token):
            raise SafeError("Configure the dedicated metadata-worker token")
        fingerprint = config.get("tls_sha256", "").replace(":", "").lower()
        if fingerprint and not re.fullmatch(r"[a-f0-9]{64}", fingerprint):
            raise SafeError("Invalid coordinator TLS fingerprint")
        self.url, self.token, self.fingerprint = url, token, fingerprint

    def request(self, endpoint, body=None):
        if self.url.scheme == "https":
            ctx = ssl.create_default_context()
            if self.fingerprint:
                ctx.check_hostname = False
                ctx.verify_mode = ssl.CERT_NONE
            ctx.minimum_version = ssl.TLSVersion.TLSv1_2
            conn = http.client.HTTPSConnection(self.url.hostname, self.url.port or 443, timeout=25, context=ctx)
        else:
            conn = http.client.HTTPConnection(self.url.hostname, self.url.port, timeout=25)
        try:
            conn.connect()
            # Verify the pinned certificate BEFORE transmitting the bearer token.
            if self.fingerprint:
                actual = hashlib.sha256(conn.sock.getpeercert(binary_form=True)).hexdigest()
                if not hmac.compare_digest(actual, self.fingerprint):
                    raise SafeError("Coordinator TLS certificate does not match configured fingerprint")
            data = json.dumps(body, ensure_ascii=False, allow_nan=False).encode() if body is not None else None
            conn.request("POST" if body is not None else "GET", PREFIX + endpoint, data,
                         {"Authorization": "Bearer " + self.token, "Content-Type": "application/json",
                          "User-Agent": f"HomeMediaMetadataWorker/{VERSION}"})
            response = conn.getresponse()
            if response.status != 200:
                raise RemoteError(response.status, retry_seconds(response))
            result = bounded_json(response, MAX_REPLY)
            if result.get("protocol") != PROTOCOL:
                raise SafeError("Coordinator protocol mismatch")
            return result
        finally:
            conn.close()


class Store:
    def __init__(self, folder):
        self.folder = Path(folder)
        self.folder.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(self.folder / "worker.sqlite")
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA synchronous=FULL")
        self.conn.execute("PRAGMA busy_timeout=5000")
        self.conn.execute("CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY,value TEXT)")
        self.conn.execute("CREATE TABLE IF NOT EXISTS jobs(job_id TEXT PRIMARY KEY,assignment TEXT NOT NULL,result TEXT,stage TEXT NOT NULL)")
        self.conn.commit()
        self.worker_id = self.get("worker_id") or "windows-" + uuid.uuid4().hex
        self.set("worker_id", self.worker_id)

    def get(self, key, default=None):
        row = self.conn.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
        return json.loads(row[0]) if row else default

    def set(self, key, value):
        with self.conn:
            self.conn.execute("INSERT OR REPLACE INTO settings VALUES (?,?)", (key, json.dumps(value)))

    def add(self, tasks, kind):
        # Validate the whole response before writing any tasks.
        if not isinstance(tasks, list) or len(tasks) > 5:
            raise SafeError("Coordinator returned too many assignments")
        checked = []
        ids = set()
        for task in tasks:
            if not isinstance(task, dict):
                raise SafeError("Invalid coordinator assignment")
            for field in ("job_id", "lease_id", "generation"):
                if not isinstance(task.get(field), str) or not re.fullmatch(r"[A-Za-z0-9_.:-]{1,128}", task[field]):
                    raise SafeError("Invalid assignment identity")
            if task.get("kind") != kind or not re.fullmatch(r"tt\d{5,12}", str(task.get("imdb", ""))):
                raise SafeError("Assignment belongs to a different metadata partition")
            if task["job_id"] in ids:
                raise SafeError("Duplicate job in coordinator response")
            ids.add(task["job_id"])
            if self.conn.execute("SELECT 1 FROM jobs WHERE job_id=?", (task["job_id"],)).fetchone():
                raise SafeError("Coordinator reissued an unfinished job")
            # Retain only the protocol identity; never execute URLs or commands.
            checked.append({k: task[k] for k in ("job_id", "lease_id", "generation", "kind", "imdb")})
        with self.conn:
            for task in checked:
                self.conn.execute("INSERT INTO jobs VALUES (?,?,NULL,'assigned')", (task["job_id"], json.dumps(task)))

    def first(self):
        row = self.conn.execute("SELECT assignment,result FROM jobs ORDER BY rowid LIMIT 1").fetchone()
        return (json.loads(row[0]), json.loads(row[1]) if row[1] else None) if row else None

    def save_result(self, job, result):
        with self.conn:
            self.conn.execute("UPDATE jobs SET result=?,stage='outbox' WHERE job_id=?", (json.dumps(result, ensure_ascii=False, allow_nan=False), job["job_id"]))

    def finish(self, job, outcome):
        # Only acknowledgements for this exact result remove the durable outbox.
        with self.conn:
            self.conn.execute("DELETE FROM jobs WHERE job_id=?", (job["job_id"],))
            key = "total_" + outcome
            value = self.get(key, 0) + 1
            self.conn.execute("INSERT OR REPLACE INTO settings VALUES (?,?)", (key, json.dumps(value)))

    def quarantine(self, job, result):
        folder = self.folder / "quarantine"
        folder.mkdir(exist_ok=True)
        if len(list(folder.glob("*.json"))) >= 64:
            raise SafeError("Quarantine is full; operator review required")
        path = folder / (hashlib.sha256(job["job_id"].encode()).hexdigest() + ".json")
        temporary = path.with_suffix(".tmp")
        temporary.write_text(json.dumps({"assignment": job, "result": result}, ensure_ascii=False), encoding="utf-8")
        temporary.replace(path)
        self.finish(job, "stale")

    def summary(self):
        return {"version": VERSION, "pending_local": self.conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0],
                "completed": self.get("total_accepted", 0) + self.get("total_duplicate", 0),
                "stale_retained": self.get("total_stale", 0), "next_provider_request": self.get("provider_after", 0)}

    def close(self):
        self.conn.close()


class Worker:
    def __init__(self, config, store, api, fetcher=fetch_metadata):
        self.config, self.store, self.api, self.fetcher = config, store, api, fetcher
        self.kind = config.get("kind", "series")
        if self.kind not in ("movie", "series"):
            raise SafeError("kind must be movie or series")
        self.interval = max(3.0, min(300.0, float(config.get("request_interval_seconds", 3))))

    def capabilities(self):
        result = self.api.request("/capabilities")
        if result.get("enabled") is not True or result.get("role") != "metadata-only":
            raise SafeError("Coordinator is not enabled for metadata-only workers")
        if result.get("exclusive_assignments") is not True or result.get("server_only_import") is not True:
            raise SafeError("Coordinator lacks required assignment/import guarantees")
        if self.kind not in result.get("kinds", []):
            raise SafeError("This metadata partition is not enabled on the coordinator")
        interval = result.get("min_request_interval_seconds", 3)
        if not isinstance(interval, (int, float)) or not 0 <= interval <= 3600:
            raise SafeError("Invalid coordinator request interval")
        self.interval = max(self.interval, interval)

    def call(self, path, job=None, **values):
        payload = {"protocol": PROTOCOL, "worker_id": self.store.worker_id, **(job or {}), **values}
        return self.api.request(path, payload)

    def renew(self, job):
        result = self.call("/renew", job)
        if result.get("job_id") != job["job_id"] or result.get("lease_id") != job["lease_id"]:
            raise SafeError("Coordinator renewal identity mismatch")
        return result.get("active") is True

    def step(self):
        row = self.store.first()
        if not row:
            result = self.call("/claim", kind=self.kind, limit=1)
            self.store.add(result.get("tasks"), self.kind)
            return max(2, min(300, float(result.get("retry_after_seconds", 10)))) if not self.store.first() else 0
        job, result = row
        if result is None:
            # Renew before fetching. Ready outbox items go directly to the
            # idempotent result endpoint, so a lost success acknowledgement can
            # still be recovered after the server has marked its job complete.
            if not self.renew(job):
                self.store.quarantine(job, result)
                LOG.warning("Expired assignment retained locally for review")
                return 0
            cooldown = self.store.get("provider_after", 0) - time.time()
            if cooldown > 0:
                return min(30, cooldown)
            try:
                meta = self.fetcher(job["kind"], job["imdb"])
                meta = metadata_identity(meta, job["kind"], job["imdb"])
                result = {"status": "ok", "metadata": meta}
            except RemoteError as error:
                result = {"status": "not_found" if error.status == 404 else "retry",
                          "reason": "provider_not_found" if error.status == 404 else "provider_http_error",
                          "provider_status": error.status, "retry_after_seconds": error.retry_after}
                if error.status in (429, 503):
                    self.store.set("provider_after", time.time() + error.retry_after)
            except (OSError, TimeoutError, http.client.HTTPException):
                result = {"status": "retry", "reason": "provider_network_error"}
            except SafeError:
                result = {"status": "retry", "reason": "provider_metadata_invalid"}
            self.store.set("provider_after", max(self.store.get("provider_after", 0), time.time() + self.interval))
            result.update({"result_id": uuid.uuid4().hex, "source": "cinemeta", "fetched_at": time.time()})
            self.store.save_result(job, result)
            LOG.info("Metadata fetched for %s %s: %s", job["kind"], job["imdb"], result["status"])
            return 0
        reply = self.call("/result", job, result=result)
        if reply.get("result_id") != result["result_id"]:
            raise SafeError("Coordinator acknowledgement identity mismatch")
        outcome = reply.get("ack")
        if outcome in ("accepted", "duplicate"):
            self.store.finish(job, outcome)
            LOG.info("Server acknowledged metadata batch item (%s)", outcome)
        elif outcome in ("stale", "rejected"):
            self.store.quarantine(job, result)
            LOG.warning("Server declined assignment; result retained locally")
        else:
            raise SafeError("Coordinator acknowledgement is invalid")
        return 0


@contextlib.contextmanager
def single_instance(folder):
    Path(folder).mkdir(parents=True, exist_ok=True)
    with (Path(folder) / "worker.lock").open("a+") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise SafeError("Another worker is already using this data folder") from None
        yield


def configure_log(folder):
    LOG.setLevel(logging.INFO)
    formatter = logging.Formatter("%(asctime)s %(levelname)s %(message)s")
    for handler in (logging.StreamHandler(), RotatingFileHandler(Path(folder) / "worker.log", maxBytes=1024 * 1024, backupCount=3)):
        handler.setFormatter(formatter)
        LOG.addHandler(handler)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=("run", "status", "demo"), default="run", nargs="?")
    parser.add_argument("--config", default="/config/config.local.json")
    parser.add_argument("--data", default="/data")
    args = parser.parse_args()
    if args.command == "demo":
        from demo import run_demo
        return run_demo()
    if args.command == "status":
        path = Path(args.data) / "worker.sqlite"
        if not path.exists():
            print("Worker has not started; no local progress database exists.")
            return 0
        # Read-only status must not create schemas or touch an active writer.
        conn = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)
        try:
            print("Pending local jobs:", conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0])
            for key, value in conn.execute("SELECT key,value FROM settings WHERE key LIKE 'total_%'"):
                print(key + ":", value)
        finally:
            conn.close()
        return 0
    Path(args.data).mkdir(parents=True, exist_ok=True)
    configure_log(args.data)
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, lambda *_: STOP.set())
    try:
        config = json.loads(Path(args.config).read_text(encoding="utf-8-sig"))
        api = ControlAPI(config)
        with single_instance(args.data):
            store = Store(args.data)
            try:
                # Bind this local outbox to exactly one server and TLS identity.
                origin = hashlib.sha256(json.dumps([config["server_url"].rstrip("/"), config.get("tls_sha256", "")]).encode()).hexdigest()
                if store.get("coordinator") not in (None, origin):
                    raise SafeError("Data folder belongs to a different coordinator; use a new folder")
                store.set("coordinator", origin)
                worker = Worker(config, store, api)
                while not STOP.is_set():
                    try:
                        worker.capabilities()
                        break
                    except RemoteError as error:
                        if error.status in (401, 403, 404):
                            raise SafeError("Coordinator is not provisioned or worker access is denied") from None
                        LOG.warning("Coordinator busy; waiting to retry")
                        STOP.wait(error.retry_after)
                    except (OSError, TimeoutError, http.client.HTTPException):
                        LOG.warning("Coordinator connection unavailable; waiting to retry")
                        STOP.wait(30)
                LOG.info("Metadata-only worker ready; partition=%s", worker.kind)
                delay = 0
                while not STOP.wait(delay):
                    if sum(p.stat().st_size for p in Path(args.data).rglob("*") if p.is_file()) > 384 * 1024 * 1024:
                        raise SafeError("Local state limit reached; operator review required")
                    try:
                        delay = worker.step()
                    except RemoteError as error:
                        if error.status in (401, 403, 404):
                            raise SafeError("Coordinator revoked access or endpoint is unavailable") from None
                        LOG.warning("Coordinator request deferred: HTTP %s", error.status)
                        delay = error.retry_after
                    except (OSError, TimeoutError, http.client.HTTPException):
                        LOG.warning("Coordinator connection lost; durable results retained for retry")
                        delay = 30
                LOG.info("Worker stopped; unacknowledged progress remains on this PC")
            finally:
                store.close()
    except (SafeError, ValueError, OSError, sqlite3.Error) as error:
        # Config contents, URL query strings, tokens and response bodies must
        # never reach the logs. Only our fixed SafeError messages are displayed.
        LOG.error("%s", str(error) if isinstance(error, SafeError) else type(error).__name__)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
