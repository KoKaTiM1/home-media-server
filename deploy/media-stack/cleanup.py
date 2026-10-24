#!/usr/bin/python3
"""Remove completed Transmission video files using Jellyfin watched status.

Fails closed if Jellyfin, Transmission, the API key, or the storage mount is
unavailable. Only files under downloads/complete are eligible. No recursive
directory deletion; multi-episode torrents are handled one video at a time.
"""
import argparse
import base64
import datetime
import fcntl
import json
import os
from pathlib import Path, PurePosixPath
import time
import urllib.error
import urllib.parse
import urllib.request

ROOT = Path('/srv/dev-disk-by-uuid-<MEDIA_FILESYSTEM_UUID>')
COMPLETE = ROOT / 'downloads/complete'
STATE = ROOT / 'appdata/cleanup/state.json'
KEY = Path('/opt/media-stack/secrets/jellyfin_api_key')
VIDEO = {'.mkv', '.mp4', '.avi', '.mov', '.m4v', '.ts', '.webm', '.mpg', '.mpeg'}
DAY = 86400
BASE = 'http://<SERVER_LAN_IP>'


def request(url, headers=None, data=None):
    body = None if data is None else json.dumps(data).encode()
    hdr = {'Content-Type': 'application/json', **(headers or {})}
    with urllib.request.urlopen(urllib.request.Request(url, body, hdr), timeout=30) as response:
        raw = response.read()
        return json.loads(raw) if raw else None


class Transmission:
    def __init__(self):
        password = Path('/opt/media-stack/secrets/transmission_password').read_text().strip()
        self.headers = {'Authorization': 'Basic ' + base64.b64encode(('server:' + password).encode()).decode()}

    def call(self, method, arguments):
        for _ in range(2):
            try:
                result = request(BASE + ':9091/transmission/rpc', self.headers,
                                 {'method': method, 'arguments': arguments})
                if result['result'] != 'success':
                    raise RuntimeError('Transmission RPC failed: ' + result['result'])
                return result.get('arguments', {})
            except urllib.error.HTTPError as error:
                if error.code != 409:
                    raise
                self.headers['X-Transmission-Session-Id'] = error.headers['X-Transmission-Session-Id']
        raise RuntimeError('Transmission session negotiation failed')


def safe_path(relative):
    path = COMPLETE / relative
    if path.is_symlink() or any(p.is_symlink() for p in path.parents):
        raise RuntimeError('Refusing symlink path')
    resolved = path.resolve()
    if not resolved.is_relative_to(COMPLETE.resolve()) or resolved == COMPLETE.resolve():
        raise RuntimeError('Path outside downloads/complete')
    return resolved


def reason(record, now, active=False):
    if active:
        return None
    watched = record.get('watched_at')
    if watched is not None:
        return 'watched for 24 hours' if now - watched >= DAY else None
    return 'unwatched for 30 days' if now - record['completed_at'] >= 30 * DAY else None


def main(apply=False):
    if not os.path.ismount(ROOT):
        raise RuntimeError('Media filesystem is not mounted')
    if not KEY.is_file() or not KEY.read_text().strip():
        print('WAITING: Jellyfin API key missing; no files deleted.')
        return
    headers = {'X-Emby-Token': KEY.read_text().strip()}
    info = request(BASE + ':8096/System/Info/Public')
    if not info.get('StartupWizardCompleted'):
        print('WAITING: Jellyfin setup incomplete; no files deleted.')
        return
    users = request(BASE + ':8096/Users', headers)
    if not users:
        raise RuntimeError('No Jellyfin users; refusing cleanup')
    folders = request(BASE + ':8096/Library/VirtualFolders', headers)
    download_library = next((folder for folder in folders if '/downloads' in folder.get('Locations', [])), None)
    if not download_library:
        print('WAITING: completed-download library missing; no files deleted.')
        return
    # Fetch every page and every user before performing any destructive action.
    items = {}
    for user in users:
        start = 0
        while True:
            params = urllib.parse.urlencode({'UserId': user['Id'], 'ParentId': download_library['ItemId'], 'Recursive': 'true',
                'IncludeItemTypes': 'Movie,Episode', 'Fields': 'Path',
                'EnableUserData': 'true', 'StartIndex': start, 'Limit': 500})
            page = request(BASE + ':8096/Items?' + params, headers)
            for item in page['Items']:
                paths = [item.get('Path', '')] + [s.get('Path', '') for s in item.get('MediaSources', [])]
                for path in paths:
                    if not path.startswith('/downloads/'):
                        continue
                    entry = items.setdefault(path, {'ids': set(), 'played': False})
                    entry['ids'].add(item['Id'])
                    entry['played'] |= bool(item.get('UserData', {}).get('Played'))
            start += len(page['Items'])
            if start >= page['TotalRecordCount']:
                break
            if not page['Items']:
                raise RuntimeError('Incomplete Jellyfin item pagination')
    sessions = request(BASE + ':8096/Sessions', headers)
    active_ids = {s['NowPlayingItem']['Id'] for s in sessions if s.get('NowPlayingItem')}
    rpc = Transmission()
    torrents = rpc.call('torrent-get', {'fields': ['id', 'hashString', 'downloadDir', 'files', 'percentDone', 'doneDate']})['torrents']
    STATE.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    state = json.loads(STATE.read_text()) if STATE.exists() else {}
    now = time.time()
    count = 0
    for torrent in torrents:
        if torrent['percentDone'] < 1 or torrent['downloadDir'].rstrip('/') != '/downloads/complete':
            continue
        for file in torrent['files']:
            relative = file['name']
            if PurePosixPath(relative).suffix.lower() not in VIDEO:
                continue
            path = safe_path(relative)
            if not path.is_file() or file['bytesCompleted'] != file['length']:
                continue
            key = torrent['hashString'] + ':' + relative
            record = state.setdefault(key, {'completed_at': now})
            library = items.get('/downloads/' + relative)
            # Wait until Jellyfin has indexed the item, so watched state is known.
            if library is None:
                continue
            if library['played']:
                record.setdefault('watched_at', now)
            else:
                record.pop('watched_at', None)
            cause = reason(record, now, bool(library['ids'] & active_ids))
            if not cause:
                continue
            print(('DELETE' if apply else 'PREVIEW') + ': ' + relative + ' (' + cause + ')')
            if apply:
                # Recheck playback immediately before stopping a torrent or deleting.
                latest = request(BASE + ':8096/Sessions', headers)
                playing = {s['NowPlayingItem']['Id'] for s in latest if s.get('NowPlayingItem')}
                if library['ids'] & playing:
                    continue
                rpc.call('torrent-stop', {'ids': [torrent['id']]})
                safe_path(relative).unlink()
                for suffix in ('.srt', '.vtt', '.ass', '.ssa', '.sub'):
                    sidecar = path.with_suffix(suffix)
                    if sidecar.is_file() and not sidecar.is_symlink():
                        safe_path(str(sidecar.relative_to(COMPLETE))).unlink()
                record['deleted_at'] = now
            count += 1
        if apply and all(not safe_path(f['name']).exists() for f in torrent['files']):
            rpc.call('torrent-remove', {'ids': [torrent['id']], 'delete-local-data': False})
    # A preview never advances timestamps or changes any persistent state.
    if apply:
        temp = STATE.with_suffix('.tmp')
        temp.write_text(json.dumps(state, indent=2) + '\n')
        temp.chmod(0o600)
        temp.replace(STATE)
    print(f'Cleanup complete: {count} eligible video files; apply={apply}.')


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--apply', action='store_true')
    args = parser.parse_args()
    with open('/run/media-cleanup.lock', 'w') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        main(args.apply)
