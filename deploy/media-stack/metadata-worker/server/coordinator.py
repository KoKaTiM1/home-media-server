"""Restricted HTTPS receiver for metadata-only assignments and handoffs."""
import argparse
import datetime as dt
import hashlib
import hmac
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import ipaddress
import json
import logging
import os
from pathlib import Path
import re
import signal
import socket
import sqlite3
import ssl
import threading
import time
from urllib.parse import urlsplit

from metadata_jobs import MetadataJobs, MetadataBusy, InvalidSubmission, PROTOCOL
from worker import metadata_identity, SafeError

LOG = logging.getLogger('metadata-coordinator')
PREFIX = '/v1/metadata-worker'
ARTWORK_HOSTS = {'images.metahub.space', 'episodes.metahub.space', 'image.tmdb.org', 'm.media-amazon.com'}
MAX_BODY = 5 * 1024 * 1024


def clean_metadata(meta, kind, imdb):
    cleaned = metadata_identity(meta, kind, imdb)
    def permitted(value):
        try:
            u = urlsplit(value)
            return (u.scheme == 'https' and u.hostname in ARTWORK_HOSTS and u.port in (None, 443)
                    and not u.username and not u.password and not u.fragment)
        except (ValueError, TypeError):
            return False
    for name in ('poster', 'background', 'logo'):
        if name in cleaned and not permitted(cleaned[name]):
            del cleaned[name]
    for item in cleaned.get('videos', []):
        if 'thumbnail' in item and not permitted(item['thumbnail']):
            del item['thumbnail']
        if 'released' in item:
            try:
                parsed = dt.date.fromisoformat(item['released'][:10])
                if not 1920 <= parsed.year <= dt.date.today().year + 5:
                    del item['released']
            except ValueError:
                del item['released']
    if cleaned.get('released'):
        try:
            parsed = dt.date.fromisoformat(cleaned['released'][:10])
            if not 1920 <= parsed.year <= dt.date.today().year + 5:
                del cleaned['released']
        except (ValueError, TypeError):
            cleaned.pop('released', None)
    return cleaned


class Coordinator:
    def __init__(self, config, token):
        self.config = config
        self.token_digest = hashlib.sha256(token.encode()).digest()
        self.jobs = MetadataJobs(config['queue_path'])
        self.jobs.initialize(config['generation'])
        self.allowed = {str(ipaddress.ip_address(x)) for x in config['allowed_clients']}

    def authenticated(self, header):
        if not isinstance(header, str) or not header.startswith('Bearer ') or len(header) > 300:
            return False
        return hmac.compare_digest(hashlib.sha256(header[7:].encode()).digest(), self.token_digest)

    def dispatch(self, method, path, payload):
        common = {'protocol': PROTOCOL}
        if method == 'GET' and path == PREFIX + '/status':
            return {**common, **self.jobs.status()}
        if method == 'GET' and path == PREFIX + '/capabilities':
            self.jobs.seen()
            return {**common, 'enabled': self.jobs.enabled(), 'role': 'metadata-only', 'kinds': ['series'],
                    'exclusive_assignments': True, 'server_only_import': True, 'min_request_interval_seconds': 3}
        if method != 'POST' or not isinstance(payload, dict) or payload.get('protocol') != PROTOCOL:
            raise InvalidSubmission('Invalid metadata request')
        worker_id = payload.get('worker_id')
        if path == PREFIX + '/claim':
            if payload.get('kind') != 'series' or payload.get('limit') != 1:
                raise InvalidSubmission('Only one series assignment is permitted')
            task = self.jobs.claim('windows', worker_id, 'series')
            return {**common, 'tasks': [task] if task else [], 'retry_after_seconds': 10}
        task = {k: payload.get(k) for k in ('job_id', 'lease_id', 'generation', 'kind', 'imdb')}
        if task['kind'] != 'series' or not re.fullmatch(r'tt\d{5,12}', str(task['imdb'])):
            raise InvalidSubmission('Invalid assignment identity')
        if path == PREFIX + '/renew':
            active = self.jobs.renew('windows', worker_id, task)
            return {**common, 'job_id': task['job_id'], 'lease_id': task['lease_id'], 'active': active}
        if path == PREFIX + '/result':
            result = payload.get('result')
            if not isinstance(result, dict):
                raise InvalidSubmission('Invalid result object')
            permitted = {'result_id', 'source', 'fetched_at', 'status', 'metadata', 'reason', 'provider_status', 'retry_after_seconds'}
            if set(result) - permitted:
                raise InvalidSubmission('Unknown result fields')
            result = dict(result)
            if result.get('status') == 'ok':
                result['metadata'] = clean_metadata(result.get('metadata'), task['kind'], task['imdb'])
            else:
                if result.get('reason') not in ('provider_not_found', 'provider_http_error', 'provider_network_error', 'provider_metadata_invalid'):
                    raise InvalidSubmission('Invalid retry reason')
                if result.get('metadata') is not None:
                    raise InvalidSubmission('Retry cannot include metadata')
            ack = self.jobs.receive('windows', worker_id, task, result)
            return {**common, 'result_id': result['result_id'], 'ack': ack}
        raise InvalidSubmission('Unknown metadata endpoint')



class Handler(BaseHTTPRequestHandler):
    protocol_version = 'HTTP/1.0'
    server_version = 'MetadataCoordinator/1'
    sys_version = ''

    def log_message(self, *_):
        pass

    def reply(self, code, payload):
        raw = json.dumps({'protocol': PROTOCOL, **payload}, allow_nan=False).encode()
        self.send_response(code)
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self.send_header('Content-Length', str(len(raw)))
        self.send_header('Cache-Control', 'no-store')
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.end_headers()
        self.wfile.write(raw)

    def do_GET(self):
        self.handle_api('GET')

    def do_POST(self):
        self.handle_api('POST')

    def handle_api(self, method):
        try:
            if not self.server.app.authenticated(self.headers.get('Authorization')):
                self.reply(401, {'error': 'Metadata worker authorization required'})
                return
            if self.headers.get('Transfer-Encoding'):
                raise InvalidSubmission('Unsupported body encoding')
            payload = None
            if method == 'POST':
                raw_length = self.headers.get('Content-Length', '')
                if not raw_length.isdigit() or not 0 < int(raw_length) <= MAX_BODY:
                    raise InvalidSubmission('Invalid request size')
                length = int(raw_length)
                raw = bytearray()
                deadline = time.monotonic() + 15
                while len(raw) < length:
                    if time.monotonic() > deadline:
                        raise InvalidSubmission('Request read timed out')
                    part = self.rfile.read1(min(65536, length - len(raw)))
                    if not part:
                        raise InvalidSubmission('Incomplete request')
                    raw.extend(part)
                payload = json.loads(raw)
            result = self.server.app.dispatch(method, self.path, payload)
            self.reply(200, result)
        except (ValueError, SafeError):
            self.reply(400, {'error': 'Invalid metadata request'})
        except MetadataBusy:
            self.reply(503, {'error': 'Metadata coordinator not enabled'})
        except sqlite3.Error:
            LOG.warning('Metadata database temporarily busy')
            self.reply(503, {'error': 'Metadata database temporarily busy'})
        except (OSError, TimeoutError):
            pass
        except Exception as error:
            LOG.error('Metadata request failed: %s', type(error).__name__)
            self.reply(500, {'error': 'Metadata request failed'})


class Server(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, app, context):
        self.app, self.context = app, context
        self.slots = threading.BoundedSemaphore(4)
        super().__init__((app.config['bind'], app.config['port']), Handler)

    def verify_request(self, request, client_address):
        return client_address[0] in self.app.allowed and self.slots.acquire(blocking=False)

    def process_request_thread(self, request, client_address):
        try:
            request.settimeout(10)
            with self.context.wrap_socket(request, server_side=True) as secured:
                super().process_request_thread(secured, client_address)
        except (OSError, ssl.SSLError):
            request.close()
        finally:
            self.slots.release()

    def handle_error(self, *_):
        LOG.warning('Metadata connection closed')


def main():
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
    parser = argparse.ArgumentParser()
    parser.add_argument('--config', required=True)
    args = parser.parse_args()
    config = json.loads(Path(args.config).read_text())
    credentials = Path(os.environ.get('CREDENTIALS_DIRECTORY', config.get('credential_directory', '.')))
    token = (credentials / 'metadata-token').read_text().strip()
    app = Coordinator(config, token)
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    context.load_cert_chain(credentials / 'metadata-cert', credentials / 'metadata-key')
    server = Server(app, context)
    stop = threading.Event()
    def finish(*_):
        stop.set()
        threading.Thread(target=server.shutdown, daemon=True).start()
    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, finish)
    LOG.info('Metadata HTTPS coordinator ready; active=%s', app.jobs.enabled())
    try:
        server.serve_forever(poll_interval=.5)
    finally:
        stop.set()
        server.server_close()


if __name__ == '__main__':
    main()
