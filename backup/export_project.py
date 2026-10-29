#!/usr/bin/env python3
"""Export an allowlisted project snapshot; never copy runtime data or secrets."""
import argparse
import base64
import datetime as dt
import hashlib
import ipaddress
import json
from pathlib import Path
import re
import shutil
import sqlite3
import subprocess
import urllib.parse
import xml.etree.ElementTree as ET

STACK = Path('/opt/media-stack')
STORAGE = Path('/srv/dev-disk-by-uuid-<MEDIA_FILESYSTEM_UUID>')
SKIP = {'.git', '__pycache__', 'obj', 'bin', 'build', '.claude', '.github'}
METADATA_WORKER_FILES = (
    '.gitignore', '.dockerignore', 'Dockerfile', 'README.md', 'START-HERE.txt',
    'PROTOCOL.md', 'NEXT-SESSION.md', 'VALIDATION.txt', 'worker.ps1',
    '01-INSTALL.cmd', '02-OFFLINE-DEMO.cmd', '03-CONFIGURE.cmd',
    '04-START.cmd', '05-STATUS.cmd', '06-LOGS.cmd', '07-STOP.cmd',
    'config/config.example.json', 'worker/worker.py', 'worker/demo.py',
    'checks/test_worker.py',
    'server/coordinator.py', 'server/README.md', 'server/config.example.json',
)


VERIFICATION_WORKER_FILES = (
    '.gitignore', 'Dockerfile', 'README.md', 'START-HERE.txt', 'config.example.json',
    'coordinator.py', 'resource_guard.py', 'verification_worker.py', 'local_verifier.py', 'package.py', 'worker.ps1',
    '01-UPDATE-AND-START.cmd', '02-START-ALL.cmd', '03-LOGS.cmd', '04-STOP-ALL.cmd',
    'checks/mock-docker.ps1', 'checks/test-launchers.ps1', 'checks/run-launcher-checks.py',
)


def private_replacements():
    profile=STACK/'secrets/export-redactions.json'
    if not profile.is_file():return {}
    values=json.loads(profile.read_text())
    if not isinstance(values,dict) or not all(isinstance(k,str) and isinstance(v,str) and v for k,v in values.items()):
        raise RuntimeError('Invalid private export replacement profile; values withheld')
    return values


def sanitize_snapshot(root):
    values=private_replacements()
    for path in root.rglob('*'):
        if not path.is_file():continue
        try:text=path.read_text()
        except UnicodeDecodeError:raise RuntimeError('Opaque file cannot be publicly exported: '+str(path.relative_to(root)))
        for name,value in sorted(values.items(),key=lambda item:len(item[1]),reverse=True):
            text=re.sub(re.escape(value),lambda _: '<'+name+'>',text,flags=re.I)
        path.write_text(text)
    (root/'deployment-values.example.json').write_text(json.dumps({name:'' for name in values},indent=2)+'\n')


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
    # Include credentials managed outside the stack's dedicated secret files.
    configuration=STORAGE/'appdata/jellyfin/config/config'
    if configuration.is_dir():
        for path in configuration.rglob('*.xml'):
            try:tree=ET.parse(path)
            except ET.ParseError:raise RuntimeError('Cannot inspect a private XML credential store')
            for field in tree.iter():
                name=field.tag.lower().replace('_','').replace('-','')
                if any(word in name for word in ('apikey','password','secret','token')) and field.text and len(field.text.strip())>=12:
                    values.add(field.text.strip().encode())
    state=Path('/var/lib/tailscale/tailscaled.state')
    if state.is_file():
        try:state_values=json.loads(state.read_text())
        except (ValueError,OSError):raise RuntimeError('Cannot inspect private Tailscale state')
        for value in state_values.values():
            if isinstance(value,str) and len(value)>=12:values.add(value.encode())
    database=STORAGE/'appdata/jellyfin/config/data/jellyfin.db'
    if database.is_file():
        with sqlite3.connect('file:'+str(database)+'?mode=ro',uri=True,timeout=3) as conn:
            tables=conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
            for (table,) in tables:
                quoted='"'+table.replace('"','""')+'"'
                columns=conn.execute('PRAGMA table_info('+quoted+')').fetchall()
                for column in columns:
                    if column[1].lower() not in {'accesstoken','apikey','token','secret','password','passwordhash'}:continue
                    field='"'+column[1].replace('"','""')+'"'
                    for (value,) in conn.execute('SELECT '+field+' FROM '+quoted):
                        if isinstance(value,str) and len(value)>=12:values.add(value.encode())
    for value in list(values):
        values.add(base64.b64encode(value))
        try:values.add(urllib.parse.quote(value.decode(),safe='').encode())
        except UnicodeDecodeError:pass
    return values


def verify(root):
    secrets = secret_values()
    identifiers=list(private_replacements().values())
    forbidden = re.compile(rb'-----BEGIN (?:OPENSSH |RSA |EC |DSA )?PRIVATE KEY-----|gh[pousr]_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{30,}|AKIA[A-Z0-9]{16}|[?&]sig=[a-f0-9]{64}|tskey-[A-Za-z0-9_-]{20,}|(?:privkey|nodekey|machinekey):[a-f0-9]{64}|https://login\.tailscale\.com/a/[A-Za-z0-9]+')
    private_network = re.compile(
        rb'(?<![0-9.])100\.(?:6[4-9]|[7-9][0-9]|1[01][0-9]|12[0-7])\.[0-9]{1,3}\.[0-9]{1,3}(?![0-9.])'
        rb'|(?:[a-z0-9-]+\.)+ts\.net\b|fd7a:115c:a1e0:[0-9a-f:]+'
        rb'|(?<![0-9.])(?:10\.(?:[0-9]{1,3}|\*)\.(?:[0-9]{1,3}|\*)\.(?:[0-9]{1,3}|\*)'
        rb'|192\.168\.(?:[0-9]{1,3}|\*)\.(?:[0-9]{1,3}|\*)'
        rb'|172\.(?:1[6-9]|2[0-9]|3[01])\.(?:[0-9]{1,3}|\*)\.(?:[0-9]{1,3}|\*))(?![0-9.])', re.I)
    failures = []
    for path in sorted(root.rglob('*')):
        if not path.is_file() or '.git' in path.relative_to(root).parts:
            continue
        data = path.read_bytes()
        network_data=re.sub(rb'(?i)(targetAbi[\x22\x27]?\s*:\s*[\x22\x27]?)(?:10\.11\.0\.0|10\.11\.11\.0)',rb'\1<SOFTWARE_ABI>',data)
        endpoints=re.findall(rb'(?:https?|tcp|udp)://((?:[0-9]{1,3}\.){3}[0-9]{1,3})|[\x22\x27]((?:[0-9]{1,3}\.){3}[0-9]{1,3}):[0-9]',data)
        unsafe_endpoint=False
        for matches in endpoints:
            try:address=ipaddress.ip_address(next(value for value in matches if value).decode())
            except ValueError:continue
            if not address.is_loopback and not address.is_unspecified:unsafe_endpoint=True
        if (any(value in data or value.decode(errors='ignore').encode('utf-16le') in data for value in secrets)
                or any(value.lower().encode() in data.lower() for value in identifiers)
                or forbidden.search(data) or private_network.search(network_data) or unsafe_endpoint):
            failures.append(str(path.relative_to(root)))
        if any(part in {'secrets', '__pycache__', 'obj', 'bin'} for part in path.relative_to(root).parts):
            failures.append(str(path.relative_to(root)))
        if path.suffix.lower() in {'.sqlite', '.db', '.key', '.pem', '.mp4', '.mkv', '.torrent', '.dll', '.zip', '.pdb'}:
            failures.append(str(path.relative_to(root)))
    if failures:
        raise RuntimeError('Snapshot rejected; review these paths (values withheld): ' + ', '.join(sorted(set(failures))))


def export(destination):
    if not private_replacements():
        raise RuntimeError('Private export-redactions.json profile required before publishing; no export created')
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
    # Optional Windows helper: exact source/docs allowlist. Never export its
    # image archive, provisioned token/config, upload outbox or local databases.
    worker = STACK / 'metadata-worker'
    if worker.is_dir():
        for name in METADATA_WORKER_FILES:
            copy(worker / name, Path('deploy/media-stack/metadata-worker') / name)
    verifier = STACK / 'verification-worker'
    if verifier.is_dir():
        for name in VERIFICATION_WORKER_FILES:
            copy(verifier / name, Path('deploy/media-stack/verification-worker') / name)
    for source in Path('/etc/systemd/system').glob('media-*'):
        if source.is_file() and source.suffix in {'.service', '.timer', '.socket'}:
            copy(source, Path('deploy/systemd') / source.name)
    copy(Path('/etc/systemd/system/docker.service.d/media-storage.conf'), 'deploy/systemd/docker.service.d/media-storage.conf')
    plugins = STORAGE / 'appdata/jellyfin/config/plugins/DynamicLibrary'
    for name in ['meta.json']:
        copy(plugins / name, Path('artifacts/DynamicLibrary') / name)
    copy(STACK / 'backup/templates/ARTIFACTS.md', 'artifacts/README.md')
    for relative in ['README.md', 'RESTORE.md', '.gitignore']:
        copy(STACK / 'backup/templates' / relative, relative)
    command = subprocess.run(['omv-confdbadm', 'read', 'conf.system.sharedfolder'], check=True, capture_output=True, text=True)
    (destination / 'deploy/omv-sharedfolders.json').write_text(json.dumps(json.loads(command.stdout), indent=2) + '\n')
    sanitize_snapshot(destination)
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
