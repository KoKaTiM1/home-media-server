"""Provider recovery and Torznab normalization; discovery is never verification."""
import base64
from email.utils import parsedate_to_datetime
import math
import re
from urllib.parse import parse_qs, urlsplit
import xml.etree.ElementTree as ET


def retry_after_seconds(headers, now):
    value = headers.get('Retry-After', '') if headers else ''
    try:
        seconds = float(value)
    except (TypeError, ValueError):
        try:
            seconds = parsedate_to_datetime(value).timestamp() - now
        except (TypeError, ValueError, OverflowError):
            return 0
    return max(0, seconds) if math.isfinite(seconds) else 0


def failure_state(prior, status, headers, spacing, now, jitter=0):
    state = dict(prior or {})
    failures = min(7, int(state.get('failures', 0)) + 1)
    base = 900 if status == 429 else 300 if status >= 500 else 60
    # Preserve a provider's longer Retry-After; never probe before it expires.
    delay = max(min(21600, base * 2 ** (failures - 1)) + jitter,
                retry_after_seconds(headers, now))
    interval = min(120, max(spacing, float(state.get('interval', spacing))) * 2)
    state.update(status=status, failures=failures, interval=interval,
                 cooldown=now + delay, next=now + interval, updated=now,
                 retry_after=retry_after_seconds(headers, now),
                 requests=int(state.get('requests', 0)) + 1,
                 errors=int(state.get('errors', 0)) + 1)
    return state


def success_state(prior, spacing, now):
    state = dict(prior or {})
    interval = max(spacing, float(state.get('interval', spacing)) / 2)
    state.update(status=200, failures=0, interval=interval, cooldown=0,
                 next=now + interval, updated=now,
                 requests=int(state.get('requests', 0)) + 1)
    return state


def xml_root(raw):
    if not isinstance(raw, bytes) or len(raw) > 2 * 1024 * 1024:
        raise ValueError('Invalid indexer response size')
    if re.search(br'<!\s*(DOCTYPE|ENTITY)', raw, re.I):
        raise ValueError('Invalid indexer XML')
    root = ET.fromstring(raw)
    if root.tag == 'error' or root.find('.//error') is not None:
        raise ValueError('Indexer reported an error')
    return root


def search_parameters(caps, key, meta):
    root = xml_root(caps)
    kind, imdb, *episode = key.split(':')
    mode = 'movie' if kind == 'movie' else 'tvsearch'
    entry = root.find('./searching/' + ('movie-search' if kind == 'movie' else 'tv-search'))
    if entry is None or entry.get('available', '').lower() != 'yes':
        raise ValueError('Indexer search unsupported')
    supported = set(entry.get('supportedParams', '').split(','))
    query = {'t': mode}
    if 'imdbid' in supported:
        query['imdbid'] = imdb
    elif 'q' in supported:
        title = meta.get('originalName') or meta.get('original_name') or meta.get('originalTitle') or meta.get('name') or meta.get('title')
        if not isinstance(title, str) or not title.strip():
            raise ValueError('Missing original search title')
        query['q'] = title[:250]
        year = str(meta.get('year') or meta.get('releaseInfo') or '')[:4]
        if kind == 'movie' and 'year' in supported and year.isdigit():query['year'] = year
    else:raise ValueError('Indexer identity search unsupported')
    if kind == 'series':
        if len(episode) != 2 or not {'season', 'ep'} <= supported:
            raise ValueError('Indexer episode search unsupported')
        query.update(season=episode[0], ep=episode[1])
    return query


def torznab_streams(raw, key):
    root = xml_root(raw)
    if root.tag != 'rss' or root.find('channel') is None:
        raise ValueError('Invalid indexer listing')
    rows = []
    for item in root.findall('./channel/item')[:100]:
        attrs = {x.get('name', '').lower(): x.get('value', '') for x in item
                 if x.tag.rsplit('}', 1)[-1] == 'attr'}
        title = item.findtext('title') or ''
        imdb = attrs.get('imdbid') or attrs.get('imdb', '')
        if imdb and imdb.removeprefix('tt') != key.split(':')[1].removeprefix('tt'):continue
        link = attrs.get('magneturl') or item.findtext('link') or ''
        infohash = attrs.get('infohash', '')
        if not infohash and link.startswith('magnet:?'):
            for xt in parse_qs(urlsplit(link).query).get('xt', []):
                if xt.lower().startswith('urn:btih:'):infohash = xt[9:];break
        if re.fullmatch('[A-Z2-7a-z]{32}', infohash):
            try:infohash = base64.b32decode(infohash.upper()).hex()
            except ValueError:continue
        if not re.fullmatch('[a-fA-F0-9]{40}', infohash):continue
        seeds = attrs.get('seeders', '0')
        seeds = seeds if seeds.isdigit() else '0'
        rows.append({'infoHash': infohash.lower(), 'title': title + '\nseeders: ' + seeds,
                     'behaviorHints': {'filename': title}, '_provider': 'jackett'})
    return rows
