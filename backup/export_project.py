#!/usr/bin/env python3
"""Export an allowlisted project snapshot; never copy runtime data or secrets."""
import argparse
import datetime as dt
import hashlib
import json
from pathlib import Path
import re
import shutil
import subprocess

STACK = Path('/opt/media-stack')
STORAGE = Path('/srv/dev-disk-by-uuid-<MEDIA_FILESYSTEM_UUID>')
SKIP = {'.git', '__pycache__', 'obj', 'bin', 'build', '.claude', '.github'}


def secret_values():
    values = set()
    for folder in [STACK / 'secrets', Path('<SERVICE_HOME>/.config/media-stack')]:
        if not folder.is_dir():
            continue
        for path in folder.iterdir():
            if not path.is_file():
                continue
            raw = path.read_bytes().strip()
            if len(raw) >= 12:
                values.add(raw)
            try:
                obj = json.loads(raw)
            except (ValueError, UnicodeDecodeError):
                continue
            def collect(value):
                if isinstance(value, dict):
                    for child in value.values():
                        collect(child)
                elif isinstance(value, list):
                    for child in value:
                        collect(child)
                elif isinstance(value, str) and len(value) >= 12:
                    values.add(value.encode())
            collect(obj)
    return values


def verify(root):
    secrets = secret_values()
    forbidden = re.compile(rb'-----BEGIN (?:OPENSSH |RSA |EC |DSA )?PRIVATE KEY-----|gh[pousr]_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{30,}|AKIA[A-Z0-9]{16}|[?&]sig=[a-f0-9]{64}')
    private_network = re.compile(
        rb'(?<![0-9.])100\.(?:6[4-9]|[7-9][0-9]|1[01][0-9]|12[0-7])\.[0-9]{1,3}\.[0-9]{1,3}(?![0-9.])'
        rb'|(?:[a-z0-9-]+\.)+ts\.net\b|fd7a:115c:a1e0:[0-9a-f:]+', re.I)
    failures = []
    for path in sorted(root.rglob('*')):
        if not path.is_file() or '.git' in path.relative_to(root).parts:
            continue
        data = path.read_bytes()
        if any(value in data for value in secrets) or forbidden.search(data) or private_network.search(data):
            failures.append(str(path.relative_to(root)))
        if any(part in {'secrets', '__pycache__', 'obj', 'bin'} for part in path.relative_to(root).parts):
            failures.append(str(path.relative_to(root)))
        if path.suffix in {'.sqlite', '.db', '.key', '.pem', '.mp4', '.mkv', '.torrent'}:
            failures.append(str(path.relative_to(root)))
    if failures:
        raise RuntimeError('Snapshot rejected; review these paths (values withheld): ' + ', '.join(sorted(set(failures))))


def export(destination):
    destination = destination.resolve()
    if destination.exists():
        raise ValueError('Choose a new destination; existing directories are never overwritten')
    destination.mkdir(parents=True, mode=0o700)
    def copy(source, relative):
        target = destination / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)
    for name in ['compose.yaml', 'cleanup.py', 'test_cleanup.py', 'configure-subtitles.py', 'configure-audio.py', 'README.md']:
        copy(STACK / name, Path('deploy/media-stack') / name)
    for folder, relative in [
        (STACK / 'catalogue', Path('deploy/media-stack/catalogue')),
        (STACK / 'omv-monitor', Path('deploy/media-stack/omv-monitor')),
        (STACK / 'omv-mobile', Path('deploy/media-stack/omv-mobile')),
        (STACK / 'vendor/dynamic-library-src', Path('plugin-source/dynamic-library')),
        (STACK / 'backup', Path('backup')),
    ]:
        for source in sorted(folder.rglob('*')):
            parts = source.relative_to(folder).parts
            if source.is_file() and not any(part in SKIP for part in parts):
                if source.suffix in {'.pyc', '.log', '.pdb', '.cache'}:
                    continue
                copy(source, relative / source.relative_to(folder))
    for source in Path('/etc/systemd/system').glob('media-*'):
        if source.is_file() and source.suffix in {'.service', '.timer'}:
            copy(source, Path('deploy/systemd') / source.name)
    copy(Path('/etc/systemd/system/docker.service.d/media-storage.conf'), 'deploy/systemd/docker.service.d/media-storage.conf')
    plugins = STORAGE / 'appdata/jellyfin/config/plugins/DynamicLibrary'
    for name in ['Jellyfin.Plugin.DynamicLibrary.dll', 'meta.json']:
        copy(plugins / name, Path('artifacts/DynamicLibrary') / name)
    copy(STACK / 'vendor/wizdom-0.2.0.0.zip', 'artifacts/wizdom-0.2.0.0.zip')
    for relative in ['README.md', 'RESTORE.md', '.gitignore']:
        copy(STACK / 'backup/templates' / relative, relative)
    command = subprocess.run(['omv-confdbadm', 'read', 'conf.system.sharedfolder'], check=True, capture_output=True, text=True)
    (destination / 'deploy/omv-sharedfolders.json').write_text(json.dumps(json.loads(command.stdout), indent=2) + '\n')
    verify(destination)
    manifest = {'created_utc': dt.datetime.now(dt.timezone.utc).isoformat(), 'scope': 'project code/configuration/plugin artifacts; excludes secrets, runtime databases and media', 'files': {}}
    for path in sorted(destination.rglob('*')):
        if path.is_file():
            manifest['files'][str(path.relative_to(destination))] = hashlib.sha256(path.read_bytes()).hexdigest()
    (destination / 'MANIFEST.json').write_text(json.dumps(manifest, indent=2) + '\n')
    verify(destination)
    print(f'Exported and checked {len(manifest["files"])} files to {destination}')


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('destination', type=Path)
    parser.add_argument('--verify-only', action='store_true')
    args = parser.parse_args()
    if args.verify_only:
        verify(args.destination)
        print('Snapshot secret/exclusion checks passed')
    else:
        export(args.destination)
