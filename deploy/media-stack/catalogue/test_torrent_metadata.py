"""Persistence requests cannot substitute for exact-source media evidence."""
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
import app


class MetadataPersistence(unittest.TestCase):
    def setUp(self):
        self.directory=tempfile.TemporaryDirectory()
        self.data=patch.object(app,'DATA',Path(self.directory.name));self.data.start()
        self.selected={'hash':'a'*40,'index':1,'filename':'Fixture.mp4','length':1234}
        self.status={'hash':'a'*40,'file_stats':[{'id':1,'path':'Fixture.mp4','length':1234}]}

    def tearDown(self):
        self.data.stop();self.directory.cleanup()

    def test_exact_file_changes_never_request_save(self):
        for field,value in [('hash','b'*40),('index',2),('filename','Wrong.mp4'),('length',9)]:
            with patch.object(app,'torr') as transport:
                self.assertFalse(app.retain_torrent_metadata({**self.selected,field:value},self.status))
                transport.assert_not_called()

    def test_acknowledgement_is_separate_from_playback_and_is_idempotent(self):
        with patch.object(app,'torr',return_value=self.status) as transport:
            self.assertTrue(app.retain_torrent_metadata(self.selected,self.status))
            self.assertTrue(app.retain_torrent_metadata(self.selected,self.status))
            self.assertEqual(transport.call_count,1)
            self.assertTrue(transport.call_args.args[0]['save_to_db'])
        with app.db() as conn:
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM playback').fetchone()[0],0)
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM torrent_metadata WHERE requested>0').fetchone()[0],1)

    def test_failed_or_wrong_response_is_retryable(self):
        with patch.object(app,'torr',side_effect=TimeoutError):
            self.assertFalse(app.retain_torrent_metadata(self.selected,self.status))
        with patch.object(app,'torr',return_value={'hash':'b'*40}):
            self.assertFalse(app.retain_torrent_metadata(self.selected,self.status))
        with app.torrent_metadata_db() as conn:
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM torrent_metadata').fetchone()[0],0)

    def test_local_download_never_enters_torrent_persistence(self):
        with patch.object(app,'torr') as transport:
            self.assertFalse(app.retain_torrent_metadata({'local':'/fixture.mp4'}))
            transport.assert_not_called()


if __name__=='__main__':unittest.main()
