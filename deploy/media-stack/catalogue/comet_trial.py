"""Bounded Comet discovery comparison. Production SQLite inputs are read-only.

Only the fixed developer endpoint is queried; returned links are never fetched.
No results are imported into the production catalogue, source queue or proof DB.
"""
import argparse
import ast
from collections import Counter, defaultdict
from contextlib import closing
import hashlib
import json
import multiprocessing
from pathlib import Path
import random
import re
import socket
import sqlite3
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from source_discovery import search_parameters, torznab_streams, series_title_matches, retry_after_seconds

ENDPOINT = 'https://comet.feels.legal/torznab/api'
MAX_BYTES = 2 * 1024 * 1024
MAX_REQUESTS = 101  # one capabilities request plus 100 title requests
KEY = re.compile(r'(movie:tt\d{5,12}|series:tt\d{5,12}:\d{1,3}:\d{1,4})')
HASH = re.compile(r'[a-fA-F0-9]{40}')
FILTER_PATH = Path('/opt/media-stack/catalogue/app.py')


def save(path, value):
    temporary = path.with_suffix(path.suffix + '.tmp')
    temporary.write_text(json.dumps(value, indent=2, ensure_ascii=False) + '\n')
    temporary.chmod(0o600)
    temporary.replace(path)


def readonly(path):
    connection = sqlite3.connect(Path(path).resolve().as_uri() + '?mode=ro', uri=True, timeout=3)
    connection.execute('PRAGMA query_only=ON')
    return connection


def load_filter(path=FILTER_PATH):
    """Load just the unchanged pure compatibility filter, never app startup."""
    raw = path.read_bytes()
    tree = ast.parse(raw)
    functions = [node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == '_filter_streams']
    if len(functions) != 1:
        raise ValueError('Compatibility filter unavailable')
    namespace = {'re': re, 'Path': Path}
    exec(compile(ast.Module(body=functions, type_ignores=[]), str(path), 'exec'), namespace)
    return namespace['_filter_streams'], hashlib.sha256(raw).hexdigest()


def extract_hashes(value, depth=0):
    found = set()
    if depth > 6:
        return found
    if isinstance(value, dict):
        for key, item in value.items():
            if key in ('hash', 'infoHash', 'info_hash') and isinstance(item, str) and HASH.fullmatch(item):
                found.add(item.lower())
            elif isinstance(item, (dict, list)):
                found.update(extract_hashes(item, depth + 1))
    elif isinstance(value, list):
        for item in value[:200]:
            found.update(extract_hashes(item, depth + 1))
    return found


def cached_row(conn, key):
    row = conn.execute('SELECT value,expires FROM cache WHERE key=?', (key,)).fetchone()
    if not row:
        return None
    try:
        return json.loads(row[0]), row[1]
    except (ValueError, TypeError):
        return None


def metadata(conn, index, key):
    kind, imdb, *_ = key.split(':')
    row = index.execute('SELECT name,original,year,votes FROM titles WHERE kind=? AND imdb=?', (kind, imdb)).fetchone()
    if not row or not row[0]:
        return None
    canonical = {'name': row[0], 'originalName': row[1] or row[0], 'releaseInfo': str(row[2] or '')}
    meta = canonical
    if kind == 'movie':
        item = conn.execute('SELECT meta FROM items WHERE key=?', (key,)).fetchone()
        if item:
            try:
                candidate = json.loads(item[0])
                if isinstance(candidate, dict):
                    meta = candidate
            except (ValueError, TypeError):
                pass
    year = str(meta.get('year') or meta.get('releaseInfo') or meta.get('released') or '')[:4]
    if kind == 'movie' and not re.fullmatch(r'\d{4}', year):
        return None
    names = [meta.get(n) for n in ('name', 'original_title', 'originalName')]
    home = conn.execute('SELECT name,original FROM home_titles WHERE kind=? AND imdb=?', (kind, imdb)).fetchone()
    alias = conn.execute('SELECT english FROM aliases WHERE imdb=?', (imdb,)).fetchone()
    if home:
        names.extend(home)
    if alias:
        names.append(alias[0])
    names = list(dict.fromkeys(n for n in names if isinstance(n, str) and n.strip()))
    if not names:
        return None
    return {'name': meta.get('name') or row[0], 'names': names, 'year': year,
            'votes': row[3] or 0, 'meta': meta}


def baseline(conn, key, config, now):
    keys = ['streams:' + key, 'mediafusion:' + key, 'resolved:' + key, 'prepared:' + key]
    keys.extend('jackett-indexer:v1:' + indexer + ':' + key
                for indexer in config.get('jackett_indexers', {}).get(key.split(':')[0], []))
    observations = []
    all_hashes, fresh_hashes = set(), set()
    for cachekey in keys:
        row = cached_row(conn, cachekey)
        if row is None:
            continue
        value, expires = row
        hashes = extract_hashes(value)
        all_hashes.update(hashes)
        if expires > now:
            fresh_hashes.update(hashes)
        # Labels only; never retain configured endpoint URLs or credentials.
        provider = ('jackett:' + cachekey.split(':')[2]) if cachekey.startswith('jackett-indexer:') else cachekey.split(':')[0]
        observations.append({'provider': provider, 'expires': expires, 'fresh': expires > now,
                             'hashes': len(hashes), 'empty': value == []})
    row = conn.execute('SELECT proof FROM validation WHERE key=?', (key,)).fetchone()
    if row and row[0]:
        try:
            all_hashes.update(extract_hashes(json.loads(row[0])))
        except (ValueError, TypeError):
            pass
    return {'captured': now, 'observations': observations,
            'all_retained_hashes': sorted(all_hashes), 'fresh_hashes': sorted(fresh_hashes)}


def vote_band(votes):
    return 'zero' if votes == 0 else 'low' if votes < 100 else 'higher'


def select_sample(candidates, controls, seed=20261112):
    """40+40 negative/partial keys; 10+10 V6 controls, distinct parent titles."""
    rng = random.Random(seed)
    selected, used = [], set()
    for control, pool, count in ((True, controls, 10), (False, candidates, 40)):
        for kind in ('movie', 'series'):
            rows = sorted((r for r in pool if r['key'].startswith(kind + ':')), key=lambda r: r['key'])
            rng.shuffle(rows)
            bands = defaultdict(list)
            for row in rows:
                bands[vote_band(row['metadata']['votes'])].append(row)
            chosen = []
            while len(chosen) < count:
                progress = False
                for band in ('zero', 'low', 'higher'):
                    while bands[band]:
                        row = bands[band].pop()
                        parent = ':'.join(row['key'].split(':')[:2])
                        if parent in used:
                            continue
                        used.add(parent); chosen.append({**row, 'control': control, 'vote_band': band})
                        progress = True
                        break
                    if len(chosen) == count:
                        break
                if not progress:
                    raise ValueError('Insufficient eligible sample identities')
            selected.extend(chosen)
    # Interleave movie/episode controls to expose either unsupported mode early.
    output = []
    for control in (True, False):
        movies = [r for r in selected if r['control'] == control and r['key'].startswith('movie:')]
        episodes = [r for r in selected if r['control'] == control and r['key'].startswith('series:')]
        for movie, episode in zip(movies, episodes):
            output.extend((movie, episode))
    return output


def prepare(catalogue, index_path, diagnostics, provider_config, output):
    output.mkdir(parents=True, exist_ok=True, mode=0o700)
    if (output / 'sample.json').exists():
        raise ValueError('Sample already exists; do not overwrite trial evidence')
    now = time.time();config = json.loads(provider_config.read_text())
    with closing(readonly(catalogue)) as conn, closing(readonly(index_path)) as index, closing(readonly(diagnostics)) as diag:
        blocked = {r[0] for r in conn.execute('SELECT key FROM verification_holds WHERE expires>?', (now,))}
        blocked.update(r[0] for r in conn.execute("SELECT key FROM manual_rechecks WHERE status='pending'"))
        negatives = [r[0] for r in diag.execute("""SELECT key FROM receipts
            WHERE code IN ('no_candidates','partial_provider_coverage','provider_recovery')
            ORDER BY finished DESC LIMIT 20000""")]
        controls = [r[0] for r in conn.execute("""SELECT key FROM validation
            WHERE state='verified' AND json_extract(proof,'$.version')>=6 ORDER BY checked DESC LIMIT 5000""")]
        pools = []
        for source in (negatives, controls):
            pool = []
            for key in dict.fromkeys(source):
                if not KEY.fullmatch(key) or key in blocked:
                    continue
                parts = key.split(':');parent = ':'.join(parts[:2])
                if any(parent == hold or key.startswith(hold + ':') for hold in blocked):
                    continue
                state = conn.execute('SELECT state FROM validation WHERE key=?', (key,)).fetchone()
                if source is negatives and state and state[0] == 'verified':
                    continue
                meta = metadata(conn, index, key)
                if meta:
                    pool.append({'key': key, 'metadata': meta})
            pools.append(pool)
        sample = select_sample(*pools)
        for row in sample:
            row['baseline'] = baseline(conn, row['key'], config, now)
    _, filter_sha = load_filter()
    data = {'schema': 1, 'prepared': now, 'endpoint': ENDPOINT, 'filter_sha': filter_sha,
            'provider_config_sha': hashlib.sha256(provider_config.read_bytes()).hexdigest(),
            'sample': sample}
    save(output / 'sample.json', data)
    print(json.dumps({'prepared': len(sample), 'controls': 20, 'negative_movies': 40, 'negative_episodes': 40}))
    return data


def matched(row, sample):
    key = sample['key'];meta = sample['metadata']
    title = row.get('behaviorHints', {}).get('filename') or row.get('title', '').split('\n')[0]
    if re.search(r'\b(sample|trailer|preview)\b', title, re.I):
        return False
    if key.startswith('series:'):
        return series_title_matches(title, key, meta['names'], meta['year'])
    if row.get('_source_imdb') != key.split(':')[1]:
        return False
    normalized = lambda s: re.sub(r'[^\w]+', ' ', s.casefold()).strip()
    text = ' ' + normalized(title) + ' '
    return (any(' ' + normalized(n) + ' ' in text for n in meta['names'])
            and bool(re.fullmatch(r'\d{4}', meta['year']) and re.search(r'(?<!\d)' + meta['year'] + r'(?!\d)', title)))


def analyze(raw, sample, compatibility_filter):
    raw_stats = {};rows = torznab_streams(raw, sample['key'], raw_stats)
    stats = {};compatible = compatibility_filter({'streams': rows}, stats)
    accepted = [r for r in compatible if matched(r, sample)]
    hashes = {r['infoHash'] for r in accepted}
    previous = set(sample['baseline']['all_retained_hashes'])
    return {'raw': raw_stats, 'normalized': len(rows), 'filter': stats,
            'identity_matched': len(accepted), 'hashes': sorted(hashes),
            'duplicate_baseline_hashes': sorted(hashes & previous),
            'additional_hashes': sorted(hashes - previous),
            'candidates': accepted}


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


def read_limited(response, deadline, clock=time.monotonic):
    chunks = [];size = 0
    while True:
        left = deadline - clock()
        if left <= 0:
            raise TimeoutError('Response budget exhausted')
        sock = getattr(getattr(getattr(response, 'fp', None), 'raw', None), '_sock', None)
        if sock is not None:
            sock.settimeout(left)
        part = response.read1(min(65536, MAX_BYTES + 1 - size))
        if clock() > deadline:
            raise TimeoutError('Response budget exhausted')
        if not part:
            return b''.join(chunks)
        size += len(part)
        if size > MAX_BYTES:
            raise ValueError('Response size exceeded')
        chunks.append(part)


def fetch_child(connection, url, timeout):
    """Disposable network process enforces total deadline including DNS/read."""
    try:
        request = urllib.request.Request(url, headers={'User-Agent': 'HomeMediaCoverageTrial/1.0', 'Accept': 'application/xml'})
        start = time.monotonic()
        with urllib.request.build_opener(NoRedirect).open(request, timeout=timeout) as response:
            raw = read_limited(response, start + timeout)
            connection.send({'status': response.status, 'body': raw})
    except urllib.error.HTTPError as error:
        connection.send({'status': error.code, 'retry_after': retry_after_seconds(error.headers, time.time())})
    except (TimeoutError, socket.timeout):
        connection.send({'error': 'timeout'})
    except urllib.error.URLError:
        connection.send({'error': 'network'})
    except ValueError:
        connection.send({'error': 'response_limit'})
    except Exception:
        connection.send({'error': 'transport'})
    finally:
        connection.close()


def fetch(query, timeout=20):
    url = ENDPOINT + '?' + urllib.parse.urlencode(query)
    ctx = multiprocessing.get_context('spawn');receive, send = ctx.Pipe(duplex=False)
    child = ctx.Process(target=fetch_child, args=(send, url, timeout));start = time.monotonic()
    child.start();send.close()
    try:
        if not receive.poll(timeout):
            return {'error': 'timeout'}
        try:
            return receive.recv()
        except EOFError:
            return {'error': 'transport'}
    finally:
        receive.close();child.join(.2)
        if child.is_alive():child.terminate();child.join(1)
        if child.is_alive():child.kill();child.join()


def result_error(result):
    status = result.get('status', 0)
    if status == 403 or status == 429:
        return 'http_' + str(status), True
    if status != 200:
        return 'http_' + str(status) if status else result.get('error', 'transport'), False
    return None, False


def summarize(sample, results, reason):
    finished = [r for r in results if r.get('phase') != 'capabilities']
    groups = {}
    for label, subset in [('controls', [r for r in finished if r['control']]), ('negative_partial', [r for r in finished if not r['control']])]:
        kinds = {}
        for kind in ('movie', 'series'):
            rs = [r for r in subset if r['key'].startswith(kind + ':')]
            kinds[kind] = {'queried': len(rs), 'valid_responses': sum(r.get('status') == 'ok' for r in rs),
                           'with_compatible_candidates': sum(r.get('identity_matched', 0) > 0 for r in rs),
                           'with_additional_hashes': sum(bool(r.get('additional_hashes')) for r in rs),
                           'additional_unique_hashes': len({h for r in rs for h in r.get('additional_hashes', [])}),
                           'errors': dict(Counter(r['status'] for r in rs if r['status'] != 'ok'))}
        groups[label] = kinds
    durations = sorted(r['seconds'] for r in finished)
    return {'schema': 1, 'finished': time.time(), 'reason': reason, 'prepared_keys': len(sample),
            'queried_keys': len(finished), 'groups': groups,
            'vote_bands': {band: {'queried': sum(r['vote_band']==band for r in finished),
                'with_compatible_candidates': sum(r['vote_band']==band and r.get('identity_matched',0)>0 for r in finished)}
                for band in ('zero','low','higher')},
            'candidate_totals': {'raw': sum(r.get('raw',{}).get('raw_rows',0) for r in finished),
                'normalized': sum(r.get('normalized',0) for r in finished),
                'matched': sum(r.get('identity_matched',0) for r in finished)},
            'baseline_cache_observations': dict(Counter('fresh' if o['fresh'] else 'stale'
                for r in finished for o in r['baseline']['observations'])),
            'latency_seconds': {'mean': sum(durations)/len(durations) if durations else None,
                                'max': max(durations) if durations else None},
            'verification_proofs_created': 0,
            'limitations': ['Discovery only; no torrent download or V6 proof.',
                           'Additional versus captured retained baseline; cache freshness is retained per title.',
                           'Distinct hashes do not establish independent upstream provenance.']}


def run(output, transport=fetch, clock=time.time, sleep=time.sleep, spacing=15, filter_path=FILTER_PATH):
    data = json.loads((output / 'sample.json').read_text());sample = data['sample']
    if (data.get('endpoint') != ENDPOINT or len(sample) != 100
        or any(not KEY.fullmatch(r['key']) for r in sample)
        or len({':'.join(r['key'].split(':')[:2]) for r in sample}) != 100
        or [r['control'] for r in sample] != [True]*20+[False]*80
        or any(sum(r['key'].startswith(kind+':') for r in sample[:20]) != 10 for kind in ('movie','series'))
        or any(sum(r['key'].startswith(kind+':') for r in sample[20:]) != 40 for kind in ('movie','series'))):
        raise ValueError('Invalid frozen sample')
    compatibility_filter, sha = load_filter(filter_path)
    if sha != data['filter_sha']:
        raise ValueError('Compatibility filter changed since sample capture')
    results_path = output / 'results.json'
    results = json.loads(results_path.read_text()) if results_path.exists() else []
    if (output / 'report.json').exists():
        return json.loads((output / 'report.json').read_text())
    state_path = output / 'state.json'
    state = json.loads(state_path.read_text()) if state_path.exists() else {'requests': 0, 'next_request': 0}
    reason = 'complete';consecutive = 0
    for previous in reversed(results):
        if previous['status'] == 'ok':break
        consecutive += 1
    if state.get('inflight'):
        # Never repeat a request whose outcome was lost during interruption.
        save(output / 'report.json', summarize(sample, results, 'interrupted_request_not_retried'))
        return json.loads((output / 'report.json').read_text())

    def request(query):
        if state['requests'] >= MAX_REQUESTS:
            return {'error': 'request_limit'}
        wait = state['next_request'] - clock()
        if wait > 0:sleep(wait)
        state['requests'] += 1;state['next_request'] = clock() + spacing
        state['inflight'] = True
        save(state_path, state)
        started = time.monotonic()
        result = transport(query)
        result['_request_seconds'] = round(time.monotonic() - started, 3)
        return result

    caps_path = output / 'caps.xml'
    if caps_path.exists():
        caps = caps_path.read_bytes()
    else:
        result = request({'t': 'caps'});error, stop = result_error(result)
        if error:
            reason = 'capabilities_' + error
            save(output / 'report.json', summarize(sample, results, reason))
            return json.loads((output / 'report.json').read_text())
        caps = result['body']
        try:
            for kind in ('movie', 'series'):
                search_parameters(caps, 'movie:tt0000001' if kind == 'movie' else 'series:tt0000001:1:1', {'name': 'Fixture'})
        except (ValueError, ET.ParseError):
            save(output / 'report.json', summarize(sample, results, 'unsupported_capabilities'))
            return json.loads((output / 'report.json').read_text())
        caps_path.write_bytes(caps);caps_path.chmod(0o600)
        state['inflight'] = False;save(state_path, state)
    done = {r['key'] for r in results}
    for row in sample:
        if row['key'] in done:continue
        controls = [r for r in results if r['control']]
        if not row['control'] and not all(any(r['key'].startswith(kind + ':') and r.get('identity_matched', 0)>0 for r in controls) for kind in ('movie', 'series')):
            reason = 'controls_no_compatible_movie_or_episode';break
        started = time.monotonic()
        result = request(search_parameters(caps, row['key'], row['metadata']['meta']))
        item = {'key': row['key'], 'control': row['control'], 'vote_band': row['vote_band'],
                'baseline': row['baseline'], 'finished': clock()}
        error, immediate = result_error(result)
        if error:
            item['status'] = error;item['retry_after'] = result.get('retry_after', 0)
        else:
            try:
                item.update(analyze(result['body'], row, compatibility_filter));item['status'] = 'ok'
            except (ValueError, ET.ParseError, TypeError, KeyError):
                item['status'] = 'invalid_schema'
        item['seconds'] = result.get('_request_seconds', round(time.monotonic() - started, 3))
        consecutive = consecutive + 1 if item['status'] != 'ok' else 0
        results.append(item);save(results_path, results)
        state['inflight'] = False;save(state_path, state)
        save(output / 'progress.json', {'updated': clock(), 'phase': 'controls' if row['control'] else 'negative_partial',
                                      'queried': len(results), 'compatible_keys': sum(r.get('identity_matched', 0)>0 for r in results),
                                      'last_status': item['status']})
        if immediate or consecutive >= 3 or item['status'] == 'request_limit':
            reason = 'stopped_' + item['status'];break
    report = summarize(sample, results, reason)
    save(output / 'report.json', report);print(json.dumps(report), flush=True)
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__);sub = parser.add_subparsers(dest='command', required=True)
    prepare_parser = sub.add_parser('prepare')
    for name in ('catalogue', 'index', 'diagnostics', 'provider-config', 'output'):
        prepare_parser.add_argument('--' + name, type=Path, required=True)
    run_parser = sub.add_parser('run');run_parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    if args.command == 'prepare':prepare(args.catalogue, args.index, args.diagnostics, args.provider_config, args.output)
    else:run(args.output)

if __name__ == '__main__':main()
