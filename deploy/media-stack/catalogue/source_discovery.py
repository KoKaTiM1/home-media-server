"""Provider recovery and Torznab normalization; discovery is never verification."""
import base64
from email.utils import parsedate_to_datetime
import math
import re
from urllib.parse import parse_qs, urlsplit
import xml.etree.ElementTree as ET


# Fixed public announce endpoints. Provider URLs never become arbitrary fetches.
DEFAULT_TRACKER='udp://tracker.opentrackr.org:1337/announce'
PUBLIC_TRACKERS=frozenset((DEFAULT_TRACKER,
 'udp://open.stealth.si:80/announce',
 'udp://tracker.torrent.eu.org:451/announce',
 'udp://exodus.desync.com:6969/announce',
 'udp://open.demonii.com:1337/announce',
 'https://tracker.gbitt.info:443/announce',
 'https://tracker.renfei.net:443/announce',
 'https://tracker.tamersunion.org:443/announce'))


def tracker_sources(values):
    """Keep at most eight distinct, exact approved tracker hints."""
    if not isinstance(values,list):return []
    out=[]
    for value in values[:128]:
        if (isinstance(value,str) and len(value)<=512 and value.startswith('tracker:')
            and value[8:] in PUBLIC_TRACKERS and value not in out):out.append(value)
        if len(out)==8:break
    return out


def magnet_parameters(link):
    if not isinstance(link,str) or not link.startswith('magnet:?') or len(link)>16384:return {}
    try:return parse_qs(urlsplit(link).query,max_num_fields=128)
    except ValueError:return {}


def magnet_tracker_sources(link):
    return tracker_sources(['tracker:'+value for value in magnet_parameters(link).get('tr',[])])


def indexer_error_details(status,body):
    """Diagnostic labels only; never retain response text or change backoff."""
    if type(status) is not int or not 100<=status<=599:return {}
    out={'adapter_http_status':status,'indexer_failure':'unknown'}
    if not isinstance(body,bytes) or len(body)>8192:return out
    text=body.decode('utf-8','replace')
    if re.search(r'(?i)\b429\s+too\s+many\s+requests\b|\bstatus\s*[:=]\s*TooManyRequests\b',text):
        out.update(indexer_failure='upstream_http_429',upstream_http_status=429)
    elif re.search(r'(?i)\bconnection reset by peer\b|\bSocketException\s*\(104\)',text):out['indexer_failure']='connection_reset'
    return out


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


def torznab_streams(raw, key, stats=None):
    root = xml_root(raw)
    if root.tag != 'rss' or root.find('channel') is None:
        raise ValueError('Invalid indexer listing')
    rows = []
    items=root.findall('./channel/item')
    if stats is not None:stats.update(raw_rows=len(items),identity_rejected=0)
    for item in items[:100]:
        attrs = {x.get('name', '').lower(): x.get('value', '') for x in item
                 if x.tag.rsplit('}', 1)[-1] == 'attr'}
        title = item.findtext('title') or ''
        imdb = attrs.get('imdbid') or attrs.get('imdb', '')
        digits=imdb.removeprefix('tt')
        bound=bool(re.fullmatch(r'[0-9]{1,12}',digits) and int(digits)>0)
        if (key.startswith('movie:') and not bound) or (bound and int(digits)!=int(key.split(':')[1][2:])):
            if stats is not None:stats['identity_rejected']+=1
            continue
        link = attrs.get('magneturl') or item.findtext('link') or ''
        infohash = attrs.get('infohash', '')
        if not infohash and link.startswith('magnet:?'):
            for xt in magnet_parameters(link).get('xt', []):
                if xt.lower().startswith('urn:btih:'):infohash = xt[9:];break
        if re.fullmatch('[A-Z2-7a-z]{32}', infohash):
            try:infohash = base64.b32decode(infohash.upper()).hex()
            except ValueError:continue
        if not re.fullmatch('[a-fA-F0-9]{40}', infohash):continue
        seeds = attrs.get('seeders', '0')
        seeds = seeds if seeds.isdigit() else '0'
        hints=magnet_tracker_sources(link)
        rows.append({'infoHash': infohash.lower(), 'title': title + '\nseeders: ' + seeds,
                     'behaviorHints': {'filename': title}, '_provider': 'jackett',
                     **({'_source_imdb':key.split(':')[1]} if bound else {}),
                     **({'sources':hints} if hints else {})})
    return rows


def series_title_matches(title, key, names, year=''):
    """Text indexers lack an IMDb binding: reject spin-offs and wrong episodes."""
    _, _, season, episode = key.split(':')
    # An explicit release-group prefix is not part of the series title.
    title = re.sub(r'^(?:\[[^\]]{1,80}\][ ._-]*)+', '', title.rsplit('/',1)[-1])
    normalize = lambda value: re.sub(r'[^\w]+', ' ', str(value).casefold()).strip()
    title = normalize(title)
    episode_pattern = rf'(?:s0*{int(season)} *e0*{int(episode)}|0*{int(season)}x0*{int(episode)})(?!\d)'
    for name in names:
        name = normalize(name)
        if not name:continue
        match = re.match(r'^'+re.escape(name)+r' *(?:(\d{4}) *)?'+episode_pattern, title)
        if match and (not match[1] or not str(year).isdigit() or match[1] == str(year)):
            return True
    return False
