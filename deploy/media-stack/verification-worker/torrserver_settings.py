"""Repair only recognizable launcher-owned TorrServer settings, with API readback.

Run after affected workers are drained. The engine owns defaults/persistence;
this tool never constructs a partial replacement for its full settings object.
"""
import argparse
import hashlib
import http.client
import json
from pathlib import Path
import re
import time
from urllib.parse import urlsplit

DESIRED = {'CacheSize': 16777216, 'PreloadCache': 0, 'ReaderReadAHead': 25,
           'UseDisk': False, 'RemoveCacheOnDrop': True, 'ConnectionsLimit': 25,
           'DisableUPNP': True, 'EnableIPv6': False, 'StoreSettingsInJson': True}
# Defaults read back from the shipped 0.3.0 backend. These identify a generated
# legacy file; a different/custom nested value is preserved, never guessed away.
LEGACY_DEFAULTS = {**DESIRED, 'CacheSize': 67108864, 'PreloadCache': 50,
                   'ReaderReadAHead': 95, 'RemoveCacheOnDrop': False,
                   'DisableUPNP': False}
LIMIT = 2 * 1024 * 1024


class SettingsError(RuntimeError):
    pass


def matches(value, expected):
    return isinstance(value, dict) and all(type(value.get(k)) is type(v) and value[k] == v
                                           for k, v in expected.items())


def read_config(path):
    if not path.exists():
        return None, None
    raw = path.read_bytes()
    if len(raw) > LIMIT:
        raise SettingsError('Settings file exceeds the bounded limit')
    try:
        value = json.loads(raw)
    except (ValueError, UnicodeError):
        raise SettingsError('Invalid settings JSON; file retained') from None
    if not isinstance(value, dict) or ('BitTorr' in value and not isinstance(value['BitTorr'], dict)):
        raise SettingsError('Invalid settings structure; file retained')
    return value, raw


def pending(path):
    return path.with_name('settings.bootstrap-v1.pending.json')


def plan(path):
    if pending(path).exists():
        raise SettingsError('Settings recovery marker present; review backups before resuming workers')
    value, raw = read_config(path)
    if value is None or value == {}:
        action = 'initialize'
    elif 'BitTorr' not in value:
        if not matches(value, DESIRED):
            raise SettingsError('Unrecognized flat settings; review without overwriting')
        action = 'migrate'
    elif matches(value['BitTorr'], DESIRED):
        action = 'current'
    elif matches(value, DESIRED) and matches(value['BitTorr'], LEGACY_DEFAULTS):
        action = 'migrate'
    else:
        action = 'preserve_custom'
    return {'action': action, 'change': action in ('initialize', 'migrate'),
            'file_sha256': hashlib.sha256(raw).hexdigest() if raw is not None else None}


class Client:
    def __init__(self, url):
        u = urlsplit(url)
        if (u.scheme != 'http' or not re.fullmatch(r'home-media-verification-torrserver(?:-[2-8])?', u.hostname or '')
                or u.port != 8090 or u.path not in ('', '/') or u.username or u.password or u.query or u.fragment):
            raise SettingsError('Invalid managed backend address')
        self.host = u.hostname

    def request(self, action, settings=None):
        body = {'action': action}
        if settings is not None:
            body['sets'] = settings
        raw = json.dumps(body, allow_nan=False).encode()
        if len(raw) > LIMIT:
            raise SettingsError('Settings request exceeds the bounded limit')
        c = http.client.HTTPConnection(self.host, 8090, timeout=5)
        try:
            c.request('POST', '/settings', raw, {'Content-Type': 'application/json'})
            response = c.getresponse()
            data = response.read(LIMIT + 1)
            if response.status != 200 or len(data) > LIMIT:
                raise SettingsError('Backend settings request rejected')
            return json.loads(data) if data else None
        finally:
            c.close()

    def get(self):
        value = self.request('get')
        if not isinstance(value, dict) or not all(k in value for k in DESIRED):
            raise SettingsError('Incomplete backend settings response')
        return value

    def set(self, value):
        self.request('set', value)


def backup(path, raw, old):
    digest = hashlib.sha256(raw).hexdigest()[:16]
    for suffix, data in (('file', raw), ('effective', json.dumps(old, sort_keys=True, allow_nan=False).encode())):
        target = path.with_name('settings.before-bootstrap-v1.' + digest + '.' + suffix + '.json')
        if target.exists():
            if target.read_bytes() != data:
                raise SettingsError('Backup conflict; settings retained')
        else:
            # Exclusive creation; never replace an earlier recovery copy.
            with target.open('xb') as f:
                f.write(data)
            target.chmod(0o600)


def ready(client):
    deadline = time.monotonic() + 30
    while True:
        try:
            return client.get()
        except (OSError, http.client.HTTPException):
            if time.monotonic() >= deadline:
                raise SettingsError('Backend settings readiness timed out') from None
            time.sleep(.5)


def apply(path, mode, client):
    if mode not in ('initialize', 'migrate'):
        raise SettingsError('Invalid repair mode')
    old = ready(client)
    config, raw = read_config(path)
    if config is None:
        raise SettingsError('Backend has not created its settings file yet')
    current_plan = plan(path)
    # Never overwrite an edited/custom nested or effective configuration.
    if matches(old, DESIRED) and matches(config.get('BitTorr'), DESIRED):
        return {'changed': False, 'status': 'already_verified'}
    allowed_file = (current_plan['action'] == 'migrate' if mode == 'migrate' else
                    matches(config.get('BitTorr'), LEGACY_DEFAULTS))
    unknown_nested = set(config.get('BitTorr', {})) - set(old)
    if not allowed_file or not matches(old, LEGACY_DEFAULTS) or unknown_nested:
        return {'changed': False, 'status': 'preserved_custom'}
    if path.read_bytes() != raw:
        raise SettingsError('Settings changed during inspection; retry after review')
    backup(path, raw, old)
    desired = {**old, **DESIRED}
    with pending(path).open('x') as f:
        json.dump({'schema': 1, 'before_sha256': hashlib.sha256(raw).hexdigest()}, f)
    pending(path).chmod(0o600)
    # API set recreates the torrent client: caller must have drained its users.
    try:
        client.set(desired)
        actual = client.get()
        after, _ = read_config(path)
        outside = {k: v for k, v in config.items() if k != 'BitTorr'}
        if (actual != desired or not matches(after.get('BitTorr'), DESIRED)
                or {k: v for k, v in after.items() if k != 'BitTorr'} != outside):
            raise SettingsError('Effective or persisted settings readback mismatch')
    except Exception:
        # Never repeat the forward set on a lost acknowledgement. Restore the
        # prior complete settings instead and keep affected workers stopped.
        try:
            client.set(old)
            restored = client.get()
            if restored != old:
                raise SettingsError('Rollback readback mismatch')
        except Exception:
            raise SettingsError('Repair failed; rollback unverified; workers must remain stopped') from None
        raise SettingsError('Repair failed; prior effective settings restored; workers remain stopped') from None
    pending(path).unlink()
    return {'changed': True, 'status': 'applied_verified'}


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('command', choices=('plan', 'apply'))
    p.add_argument('--config', type=Path, required=True)
    p.add_argument('--mode', choices=('initialize', 'migrate'))
    p.add_argument('--url')
    a = p.parse_args()
    try:
        if a.command == 'plan':
            result = plan(a.config)
        else:
            if not a.mode or not a.url:
                raise SettingsError('Repair requires a mode and managed backend URL')
            result = apply(a.config, a.mode, Client(a.url))
        print(json.dumps(result), flush=True)
    except Exception as error:
        # No backend body, path, endpoint or exception detail leaks to console.
        message = str(error) if isinstance(error, SettingsError) else 'Settings operation failed; configuration retained'
        print(json.dumps({'error': message}), flush=True)
        raise SystemExit(1)


if __name__ == '__main__':
    main()
