import importlib.util
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import json

spec = importlib.util.spec_from_file_location('cleanup', '/opt/media-stack/cleanup.py')
c = importlib.util.module_from_spec(spec)
spec.loader.exec_module(c)


class CleanupTests(unittest.TestCase):
    def test_policy_boundaries(self):
        self.assertIsNone(c.reason({'completed_at': 0}, 30*c.DAY-1))
        self.assertEqual(c.reason({'completed_at': 0}, 30*c.DAY), 'unwatched for 30 days')
        self.assertIsNone(c.reason({'completed_at': 0, 'watched_at': 30*c.DAY}, 30*c.DAY+1))
        self.assertEqual(c.reason({'completed_at': 0, 'watched_at': 0}, c.DAY), 'watched for 24 hours')
        self.assertIsNone(c.reason({'completed_at': 0}, 100*c.DAY, active=True))

    def test_paths_cannot_escape(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            complete = root / 'complete'
            complete.mkdir()
            (complete / 'link').symlink_to(root, target_is_directory=True)
            with patch.object(c, 'COMPLETE', complete):
                self.assertRaises(RuntimeError, c.safe_path, '../outside.mp4')
                self.assertRaises(RuntimeError, c.safe_path, 'link/outside.mp4')
                self.assertEqual(c.safe_path('safe.mp4'), complete / 'safe.mp4')

    def test_episode_cleanup_preserves_other_episode_and_playing(self):
        for active, apply in ((False, False), (True, True), (False, True)):
            with self.subTest(active=active, apply=apply), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                complete = root / 'complete'
                complete.mkdir()
                for name in ('ep1.mkv', 'ep2.mkv'):
                    (complete / name).write_bytes(b'media')
                keyfile = root / 'key'
                keyfile.write_text('test-only')
                state = root / 'state.json'
                state.write_text(json.dumps({'hash:ep1.mkv': {'completed_at': 0, 'watched_at': 0}}))
                torrent = {'id': 1, 'hashString': 'hash', 'downloadDir': '/downloads/complete',
                           'percentDone': 1, 'files': [{'name': n, 'length': 5, 'bytesCompleted': 5}
                                                     for n in ('ep1.mkv', 'ep2.mkv')]}
                calls = []
                class RPC:
                    def call(self, method, args):
                        calls.append(method)
                        return {'torrents': [torrent]} if method == 'torrent-get' else {}
                def api(url, *args):
                    if url.endswith('/Public'):
                        return {'StartupWizardCompleted': True}
                    if url.endswith('/Users'):
                        return [{'Id': 'user'}]
                    if url.endswith('/Library/VirtualFolders'):
                        return [{'ItemId':'downloads','Locations':['/downloads']}]
                    if '/Items?' in url:
                        return {'TotalRecordCount': 2, 'Items': [
                            {'Id': 'one', 'Path': '/downloads/ep1.mkv', 'UserData': {'Played': True}},
                            {'Id': 'two', 'Path': '/downloads/ep2.mkv', 'UserData': {'Played': False}}]}
                    if url.endswith('/Sessions'):
                        return [{'NowPlayingItem': {'Id': 'one'}}] if active else []
                    raise AssertionError(url)
                with patch.multiple(c, ROOT=root, COMPLETE=complete, STATE=state, KEY=keyfile), \
                     patch.object(c.os.path, 'ismount', return_value=True), \
                     patch.object(c, 'request', side_effect=api), \
                     patch.object(c, 'Transmission', RPC), patch.object(c.time, 'time', return_value=2*c.DAY):
                    c.main(apply)
                self.assertTrue((complete / 'ep2.mkv').exists())
                self.assertEqual((complete / 'ep1.mkv').exists(), active or not apply)
                self.assertEqual('torrent-stop' in calls, apply and not active)
                self.assertNotIn('torrent-remove', calls)


if __name__ == '__main__':
    unittest.main()
