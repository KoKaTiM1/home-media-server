import hashlib
import http.client
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import socket
import ssl
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
from io import BytesIO

root = Path(__file__).resolve().parents[1]
sys.path.insert(0, str((root if (root / "worker").is_dir() else root / "package") / "worker"))
from worker import ControlAPI, MAX_META, PROTOCOL, RemoteError, SafeError, Store, Worker, fetch_metadata, metadata_identity, single_instance
from demo import DemoCoordinator, run_demo

TOKEN = "test-token-only-never-for-production-123456"


class WorkerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = Store(self.temp.name)
        self.api = DemoCoordinator()
        self.calls = []
        self.worker = Worker({"kind": "series"}, self.store, self.api, self.provider)

    def tearDown(self):
        self.store.close()
        self.temp.cleanup()

    def provider(self, kind, imdb):
        self.calls.append(imdb)
        return {"id": imdb, "type": kind, "name": "Example", "videos": []}

    def test_offline_demo_recovers_after_restart_and_lost_ack(self):
        self.assertEqual(run_demo(), 0)

    def test_outbox_persists_until_matching_server_ack(self):
        self.worker.step()
        self.worker.step()
        result_id = self.store.first()[1]["result_id"]
        with self.assertRaises(OSError):
            self.worker.step()
        self.store.close()
        self.store = Store(self.temp.name)
        self.worker = Worker({"kind": "series"}, self.store, self.api, self.provider)
        self.assertEqual(self.store.first()[1]["result_id"], result_id)
        self.worker.step()
        self.assertEqual(self.calls, ["tt0000001"])
        self.assertEqual(len(self.api.received), 1)
        self.assertIsNone(self.store.first())

    def test_ready_result_does_not_need_renewal_to_recover_lost_ack(self):
        self.worker.step()
        self.worker.step()
        original = self.api.request
        def request(path, payload=None):
            if path == "/renew":
                raise AssertionError("Acknowledged lease need not remain active")
            return original(path, payload)
        self.api.request = request
        self.api.simulate_lost_ack = False
        self.worker.step()
        self.assertIsNone(self.store.first())

    def test_wrong_ack_keeps_result_and_does_not_refetch(self):
        self.worker.step()
        self.worker.step()
        original = self.api.request
        self.api.request = lambda path, payload=None: ({"protocol": PROTOCOL, "result_id": "wrong", "ack": "accepted"}
                                                      if path == "/result" else original(path, payload))
        with self.assertRaises(SafeError):
            self.worker.step()
        self.assertIsNotNone(self.store.first()[1])
        self.assertEqual(len(self.calls), 1)

    def test_expired_lease_never_fetches_and_is_retained_for_review(self):
        self.worker.step()
        original = self.api.request
        def request(path, payload=None):
            result = original(path, payload)
            if path == "/renew":
                result["active"] = False
            return result
        self.api.request = request
        self.worker.step()
        self.assertEqual(self.calls, [])
        self.assertIsNone(self.store.first())
        self.assertEqual(len(list((Path(self.temp.name) / "quarantine").glob("*.json"))), 1)

    def test_provider_rate_limit_survives_restart(self):
        self.worker.step()
        self.worker.fetcher = lambda *_: (_ for _ in ()).throw(RemoteError(429, 600))
        self.worker.step()
        result = self.store.first()[1]
        self.assertEqual(result["status"], "retry")
        self.assertEqual(result["provider_status"], 429)
        self.assertEqual(result["retry_after_seconds"], 600)
        self.assertNotIn("metadata", result)
        self.store.close()
        self.store = Store(self.temp.name)
        self.assertGreater(self.store.get("provider_after"), time.time() + 590)

    def test_wrong_provider_identity_is_retry_not_metadata_success(self):
        self.worker.step()
        self.worker.fetcher = lambda *_: {"id": "tt9999999", "type": "series", "name": "Wrong"}
        self.worker.step()
        result = self.store.first()[1]
        self.assertEqual(result["status"], "retry")
        self.assertEqual(result["reason"], "provider_metadata_invalid")
        self.assertNotIn("metadata", result)

    def test_wrong_partition_and_duplicate_assignments_are_atomic(self):
        task = {"job_id": "1", "lease_id": "1", "generation": "1", "kind": "series", "imdb": "tt0000001"}
        with self.assertRaises(SafeError):
            self.store.add([task, {**task, "job_id": "2", "kind": "movie"}], "series")
        self.assertIsNone(self.store.first())
        with self.assertRaises(SafeError):
            self.store.add([task, task], "series")
        self.assertIsNone(self.store.first())

    def test_capability_guarantees_required(self):
        self.api.request = lambda *_: {"protocol": PROTOCOL, "enabled": True, "role": "metadata-only", "kinds": ["series"]}
        with self.assertRaises(SafeError):
            self.worker.capabilities()

    def test_single_instance_rejects_second_worker(self):
        with single_instance(self.temp.name):
            with self.assertRaises(SafeError):
                with single_instance(self.temp.name):
                    self.fail("Second instance acquired the same lock")

    def test_metadata_does_not_include_streams_or_foreign_episode_ids(self):
        result = metadata_identity({"id": "tt0000001", "type": "series", "name": "Example", "streams": ["secret"],
                                    "poster": "http://127.0.0.1/private", "videos": [
                                       {"id": "tt9999999:1:1", "season": 1, "episode": 1},
                                       {"id": "tt0000001:1:2", "season": 1, "episode": 2, "streams": ["secret"]}]},
                                   "series", "tt0000001")
        self.assertNotIn("streams", result)
        self.assertNotIn("poster", result)
        self.assertEqual([v["episode"] for v in result["videos"]], [2])
        self.assertNotIn("streams", result["videos"][0])

    def test_coordinator_config_refuses_cleartext_and_embedded_credentials(self):
        for url in ("http://example.org:8123", "https://user:pass@example.org", "https://example.org/private?key=x"):
            with self.assertRaises(SafeError):
                ControlAPI({"server_url": url, "token": TOKEN})

    def test_provider_follows_current_cinemeta_redirect_for_same_title(self):
        hosts = []
        paths = []
        class Response(BytesIO):
            def __init__(self, status, body=b"", location=""):
                super().__init__(body)
                self.status, self.location = status, location
            def getheader(self, key, default=""):
                return self.location if key == "Location" else default
        class Connection:
            def __init__(self, host, **_):
                hosts.append(host)
                self.host = host
            def request(self, method, path, **_):
                paths.append(path)
            def getresponse(self):
                if self.host == 'v3-cinemeta.strem.io':
                    return Response(307, location='https://cinemeta-live.strem.io/meta/series/tt0000001.json')
                return Response(200, json.dumps({'meta': {'id': 'tt0000001', 'type': 'series', 'name': 'Example'}}).encode())
            def close(self):
                pass
        with patch('worker.http.client.HTTPSConnection', Connection):
            result = fetch_metadata('series', 'tt0000001')
        self.assertEqual(result['id'], 'tt0000001')
        self.assertEqual(hosts, ['v3-cinemeta.strem.io', 'cinemeta-live.strem.io'])
        self.assertEqual(paths, ['/meta/series/tt0000001.json'] * 2)

    def test_provider_redirect_cannot_change_host_port_or_title(self):
        locations = ['https://example.org/meta/series/tt0000001.json',
                     'https://cinemeta-live.strem.io/meta/series/tt9999999.json',
                     'https://cinemeta-live.strem.io:123/meta/series/tt0000001.json',
                     'http://cinemeta-live.strem.io/meta/series/tt0000001.json',
                     'https://cinemeta-live.strem.io/meta/series/tt0000001.json?token=other']
        for location in locations:
            with self.subTest(location=location):
                with patch('worker.http.client.HTTPSConnection') as connection:
                    response = connection.return_value.getresponse.return_value
                    response.status = 307
                    response.getheader.return_value = location
                    with self.assertRaises(SafeError):
                        fetch_metadata('series', 'tt0000001')
                    self.assertEqual(connection.call_count, 1)


class TransportTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory()
        key = Path(cls.temp.name) / "key.pem"
        cert = Path(cls.temp.name) / "cert.pem"
        subprocess.run(["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-days", "1",
                        "-subj", "/CN=metadata-worker-test", "-keyout", str(key), "-out", str(cert)],
                       check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        cls.pin = hashlib.sha256(ssl.PEM_cert_to_DER_cert(cert.read_text())).hexdigest()
        cls.headers = []
        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_):
                pass
            def handle(self):
                try:
                    super().handle()
                except (BrokenPipeError, ConnectionResetError, ssl.SSLError):
                    pass  # Expected when the client rejects a TLS certificate.
            def do_GET(self):
                cls.headers.append(self.headers.get("Authorization"))
                raw = json.dumps({"protocol": PROTOCOL}).encode()
                self.send_response(200)
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.load_cert_chain(cert, key)
        cls.server.socket = context.wrap_socket(cls.server.socket, server_side=True)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.url = "https://127.0.0.1:" + str(cls.server.server_port)

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(5)
        cls.temp.cleanup()

    def test_pinned_tls_allows_known_self_signed_certificate(self):
        api = ControlAPI({"server_url": self.url, "token": TOKEN, "tls_sha256": self.pin})
        self.assertEqual(api.request("/capabilities")["protocol"], PROTOCOL)
        self.assertEqual(self.headers[-1], "Bearer " + TOKEN)

    def test_pin_mismatch_sends_no_authorization_or_http_request(self):
        before = len(self.headers)
        api = ControlAPI({"server_url": self.url, "token": TOKEN, "tls_sha256": "0" * 64})
        with self.assertRaises(SafeError):
            api.request("/capabilities")
        self.assertEqual(len(self.headers), before)

    def test_standard_tls_rejects_untrusted_certificate(self):
        api = ControlAPI({"server_url": self.url, "token": TOKEN})
        before = len(self.headers)
        with self.assertRaises(ssl.SSLCertVerificationError):
            api.request("/capabilities")
        self.assertEqual(len(self.headers), before)


if __name__ == "__main__":
    unittest.main(verbosity=2)
