#!/usr/bin/env python3
"""Verify bootstrap against a real TorrServer API, with scratch-only mounts."""
import argparse
import json
from pathlib import Path
import subprocess
import tempfile
import uuid


def check(repository, torrent_image, python_image='home-media-tests:1', entrypoint='/usr/bin/torrserver'):
    helper = Path(repository) / 'deploy/media-stack/verification-worker/torrserver_settings.py'
    if not helper.is_file():
        raise RuntimeError('Settings helper missing from snapshot')
    network = 'settings-regression-' + uuid.uuid4().hex
    backend = network + '-engine'
    host = 'home-media-verification-torrserver'
    def run(args, expected=0):
        result = subprocess.run(args, capture_output=True, text=True, timeout=75)
        if result.returncode != expected:
            # Fixtures contain no credentials, yet keep output bounded.
            raise RuntimeError('Settings engine check failed: ' + result.stdout[-1500:] + result.stderr[-1500:])
        return result.stdout
    def cli(folder, command, mode=None, expected=0):
        args = ['docker', 'run', '--rm', '--user', '10001:10001', '--network', 'none' if command == 'plan' else network,
                '--read-only', '--memory', '128m', '--cap-drop', 'ALL', '--security-opt', 'no-new-privileges',
                '--mount', f'type=bind,src={helper},dst=/settings-tool.py,readonly',
                '--mount', f'type=bind,src={folder},dst=/state' + (',readonly' if command == 'plan' else ''),
                '--entrypoint', 'python', python_image, '/settings-tool.py', command, '--config', '/state/settings.json']
        if mode:
            args += ['--mode', mode, '--url', 'http://' + host + ':8090']
        return json.loads(run(args, expected))
    def get():
        code = "import importlib.util,json;s=importlib.util.spec_from_file_location('tool','/settings-tool.py');m=importlib.util.module_from_spec(s);s.loader.exec_module(m);print(json.dumps(m.ready(m.Client('http://" + host + ":8090'))))"
        return json.loads(run(['docker', 'run', '--rm', '--network', network, '--read-only', '--memory', '128m',
                              '--cap-drop', 'ALL', '--security-opt', 'no-new-privileges', '--mount',
                              f'type=bind,src={helper},dst=/settings-tool.py,readonly', '--entrypoint', 'python', python_image, '-c', code]))
    def start(folder):
        run(['docker', 'run', '-d', '--name', backend, '--network', network, '--network-alias', host,
             '--memory', '512m', '--cpus', '1', '--pids-limit', '128', '--cap-drop', 'ALL', '--security-opt', 'no-new-privileges',
             '--tmpfs', '/run/settings-check:rw,size=64m', '--mount', f'type=bind,src={folder},dst=/opt/ts/config',
             '--entrypoint', entrypoint, torrent_image, '--path', '/opt/ts/config', '--logpath', '/run/settings-check/server.log',
             '--port', '8090', '--torrentsdir', '/run/settings-check/torrents'])
    def stop():
        subprocess.run(['docker', 'rm', '-fv', backend], capture_output=True, timeout=30)
    run(['docker', 'network', 'create', network])
    try:
        with tempfile.TemporaryDirectory(prefix='settings-engine-regression-') as temporary:
            root = Path(temporary)
            fresh = root / 'fresh';fresh.mkdir();fresh.chmod(0o777)
            assert cli(fresh, 'plan')['action'] == 'initialize'
            start(fresh)
            defaults = get()
            assert defaults['CacheSize'] == 67108864 and defaults['PreloadCache'] == 50
            assert cli(fresh, 'apply', 'initialize')['status'] == 'applied_verified'
            effective = get()
            assert effective['CacheSize'] == 16777216 and effective['PreloadCache'] == 0
            assert effective['ReaderReadAHead'] == 25 and effective['DisableUPNP'] is True
            assert effective['RemoveCacheOnDrop'] is True
            owned = ('CacheSize', 'PreloadCache', 'ReaderReadAHead', 'UseDisk', 'RemoveCacheOnDrop',
                     'ConnectionsLimit', 'DisableUPNP', 'EnableIPv6', 'StoreSettingsInJson')
            assert {k: v for k, v in defaults.items() if k not in owned} == {k: v for k, v in effective.items() if k not in owned}
            assert json.loads((fresh / 'settings.json').read_text())['BitTorr']['CacheSize'] == 16777216
            assert cli(fresh, 'plan')['action'] == 'current'
            assert cli(fresh, 'apply', 'initialize')['status'] == 'already_verified'
            assert len(list(fresh.glob('*.file.json'))) == 1
            stop()
            legacy = root / 'legacy';legacy.mkdir();legacy.chmod(0o777)
            desired = {k: effective[k] for k in owned}
            sentinel = {'fixture': ['preserve-root-fields']}
            (legacy / 'settings.json').write_text(json.dumps({**desired, 'FixturePreserve': sentinel}))
            assert cli(legacy, 'plan')['action'] == 'migrate'
            start(legacy)
            old_effective = get();assert old_effective['CacheSize'] == 67108864
            original = (legacy / 'settings.json').read_bytes()
            assert cli(legacy, 'apply', 'migrate')['status'] == 'applied_verified'
            assert get() == {**old_effective, **desired}
            assert json.loads((legacy / 'settings.json').read_text())['FixturePreserve'] == sentinel
            assert list(legacy.glob('*.file.json'))[0].read_bytes() == original
            stop()
            custom = root / 'custom';custom.mkdir();custom.chmod(0o777)
            customized = {**defaults, 'CacheSize': 134217728}
            (custom / 'settings.json').write_text(json.dumps({**desired, 'BitTorr': customized, 'FixturePreserve': sentinel}))
            start(custom)
            before = get();raw = (custom / 'settings.json').read_bytes()
            assert cli(custom, 'plan')['action'] == 'preserve_custom'
            assert cli(custom, 'apply', 'migrate')['status'] == 'preserved_custom'
            assert get() == before and (custom / 'settings.json').read_bytes() == raw
            assert not list(custom.glob('*.file.json'))
            stop()
        print('PASS: real-engine fresh/legacy/custom/idempotence/effective+persistent readback and backup preservation', flush=True)
    except Exception:
        detail = subprocess.run(['docker', 'exec', backend, 'tail', '-c', '6000', '/run/settings-check/server.log'], capture_output=True, text=True, timeout=10)
        state = subprocess.run(['docker', 'inspect', backend, '--format', '{{json .State}}'], capture_output=True, text=True, timeout=10)
        logs = subprocess.run(['docker', 'logs', backend], capture_output=True, text=True, timeout=10)
        print('Engine diagnostic: '+state.stdout[-600:]+' '+logs.stdout[-1500:]+' '+logs.stderr[-1500:]+' '+detail.stdout[-2500:], flush=True)
        raise
    finally:
        stop();subprocess.run(['docker', 'network', 'rm', network], capture_output=True, timeout=30)


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--repository', type=Path, required=True)
    p.add_argument('--torrserver-image', required=True)
    p.add_argument('--python-image', default='home-media-tests:1')
    p.add_argument('--entrypoint', default='/usr/bin/torrserver')
    a = p.parse_args();check(a.repository, a.torrserver_image, a.python_image, a.entrypoint)


if __name__ == '__main__':main()
