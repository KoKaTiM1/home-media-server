"""Cleanup may inspect loaded readers but must not reactivate saved torrents."""
import time
import unittest
import urllib.error
from unittest.mock import patch
from test_app import a

class CleanupTests(unittest.TestCase):
    def setUp(self):
        self.key='movie:tt0000001';self.hash='a'*40
        for obj,name,value in ((a,'background_torrent_records',[(time.time(),self.key,self.hash)]),):
            p=patch.object(obj,name,return_value=value);p.start();self.addCleanup(p.stop)
        p=patch.dict(a.FOREGROUND_CLEANUP,{'protected':set(),'hashes':set()});p.start();self.addCleanup(p.stop)
        p=patch.object(a,'torr_headers',return_value={});p.start();self.addCleanup(p.stop)
        self.fetch=patch.object(a,'fetch');self.f=self.fetch.start();self.addCleanup(self.fetch.stop)
        p=patch.object(a,'torr');self.t=p.start();self.addCleanup(p.stop)
        p=patch.object(a,'retain_background_torrent_records');self.ret=p.start();self.addCleanup(p.stop)
    def test_dormant_record_does_not_activate_or_drop(self):
        self.f.side_effect=urllib.error.HTTPError('fixture',404,'missing',None,None)
        a.cleanup_background_torrents();self.t.assert_not_called();self.ret.assert_called_once_with({self.hash})
        self.assertTrue(all(args[0][0].endswith('/cache') for args in self.f.call_args_list))
    def test_reader_protects_loaded_torrent(self):
        self.f.return_value={'Readers':[{}]};a.cleanup_background_torrents();self.t.assert_not_called();self.ret.assert_called_once_with(set())
    def test_idle_loaded_torrent_only_drops_without_loading_status(self):
        self.f.return_value={};a.cleanup_background_torrents()
        self.assertEqual(self.f.call_count,2);self.t.assert_called_once_with({'action':'drop','hash':self.hash},timeout=.75)
    def test_unreachable_cache_retains_record(self):
        self.f.side_effect=urllib.error.HTTPError('fixture',503,'unavailable',None,None)
        a.cleanup_background_torrents();self.t.assert_not_called();self.ret.assert_called_once_with(set())
    def test_new_foreground_protection_is_rechecked_before_drop(self):
        def inspect(*args,**kwargs):a.FOREGROUND_CLEANUP['hashes'].add(self.hash);return {}
        self.f.side_effect=inspect;a.cleanup_background_torrents();self.t.assert_not_called()

if __name__=='__main__':unittest.main()
