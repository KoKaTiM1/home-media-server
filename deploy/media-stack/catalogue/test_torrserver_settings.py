"""Launcher settings regressions: preserve custom state, verify effective values."""
import copy
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

SOURCE = Path(__file__).resolve().parents[1] / 'verification-worker/torrserver_settings.py'
spec = importlib.util.spec_from_file_location('settings_bootstrap', SOURCE)
s = importlib.util.module_from_spec(spec);spec.loader.exec_module(s)


class Backend:
    def __init__(self, path, value):
        self.path = path;self.value = copy.deepcopy(value);self.sets = [];self.bad_readback = False

    def get(self):
        result = copy.deepcopy(self.value)
        if self.bad_readback and len(self.sets) == 1:
            result['CacheSize'] = 123
        return result

    def set(self, value):
        self.sets.append(copy.deepcopy(value));self.value = copy.deepcopy(value)
        data = json.loads(self.path.read_text());data['BitTorr'] = self.value
        self.path.write_text(json.dumps(data))


class SettingsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory();self.path = Path(self.temp.name) / 'settings.json'
        self.defaults = {**s.LEGACY_DEFAULTS, 'CustomNetworkField': {'keep': [1, 2]}, 'PrivateValue': 'not-for-console'}

    def tearDown(self):
        self.temp.cleanup()

    def write(self, value):
        self.path.write_text(json.dumps(value))

    def test_fresh_uses_full_effective_defaults_and_persists_nested(self):
        self.assertEqual(s.plan(self.path)['action'], 'initialize')
        self.write({'BitTorr': self.defaults, 'Viewed': {'sentinel': 42}})
        backend = Backend(self.path, self.defaults)
        self.assertTrue(s.apply(self.path, 'initialize', backend)['changed'])
        result = json.loads(self.path.read_text())
        self.assertEqual(result['Viewed'], {'sentinel': 42})
        self.assertEqual(result['BitTorr']['CustomNetworkField'], self.defaults['CustomNetworkField'])
        self.assertTrue(s.matches(result['BitTorr'], s.DESIRED))
        self.assertFalse(s.pending(self.path).exists())

    def test_known_legacy_flat_and_generated_defaults_are_repaired_with_backup(self):
        self.write({**s.DESIRED, 'BitTorr': self.defaults, 'Unrelated': ['preserve']})
        before = self.path.read_bytes();self.assertEqual(s.plan(self.path)['action'], 'migrate')
        backend = Backend(self.path, self.defaults)
        self.assertEqual(s.apply(self.path, 'migrate', backend)['status'], 'applied_verified')
        copies = list(self.path.parent.glob('settings.before-bootstrap-v1.*.file.json'))
        self.assertEqual(len(copies), 1);self.assertEqual(copies[0].read_bytes(), before)
        self.assertEqual(json.loads(self.path.read_text())['Unrelated'], ['preserve'])
        self.assertEqual(s.plan(self.path)['action'], 'current')
        self.assertEqual(s.apply(self.path, 'migrate', backend)['status'], 'already_verified')
        self.assertEqual(len(backend.sets), 1)
        self.assertEqual(len(list(self.path.parent.glob('*.file.json'))), 1)

    def test_flat_legacy_before_engine_initialization_is_recognized(self):
        self.write(s.DESIRED);self.assertEqual(s.plan(self.path)['action'], 'migrate')
        backend = Backend(self.path, self.defaults)
        s.apply(self.path, 'migrate', backend)
        self.assertTrue(s.matches(json.loads(self.path.read_text())['BitTorr'], s.DESIRED))

    def test_custom_nested_is_untouched_even_with_old_flat_signature(self):
        custom = {**self.defaults, 'CacheSize': 128 * 1024**2}
        self.write({**s.DESIRED, 'BitTorr': custom});before = self.path.read_bytes()
        self.assertEqual(s.plan(self.path)['action'], 'preserve_custom')
        backend = Backend(self.path, custom)
        self.assertEqual(s.apply(self.path, 'migrate', backend)['status'], 'preserved_custom')
        self.assertEqual(self.path.read_bytes(), before);self.assertEqual(backend.sets, [])

    def test_existing_nested_defaults_without_ownership_are_not_overridden(self):
        self.write({'BitTorr': self.defaults})
        self.assertEqual(s.plan(self.path)['action'], 'preserve_custom')
        backend = Backend(self.path, self.defaults)
        self.assertFalse(s.apply(self.path, 'migrate', backend)['changed'])
        self.assertEqual(backend.sets, [])

    def test_changed_effective_custom_values_are_not_overwritten(self):
        self.write({**s.DESIRED, 'BitTorr': self.defaults})
        backend = Backend(self.path, {**self.defaults, 'ConnectionsLimit': 77})
        self.assertEqual(s.apply(self.path, 'migrate', backend)['status'], 'preserved_custom')
        self.assertEqual(backend.sets, [])

    def test_unknown_nested_fields_not_in_engine_schema_are_preserved(self):
        self.write({**s.DESIRED, 'BitTorr': {**self.defaults, 'FutureField': 42}})
        backend = Backend(self.path, self.defaults)
        before = self.path.read_bytes()
        self.assertEqual(s.apply(self.path, 'migrate', backend)['status'], 'preserved_custom')
        self.assertEqual(self.path.read_bytes(), before);self.assertEqual(backend.sets, [])

    def test_invalid_or_unrecognized_inputs_fail_before_any_mutation(self):
        for text in ('broken', '[]', '{"BitTorr":3}', '{"CacheSize":123}'):
            self.path.write_text(text)
            with self.assertRaises(s.SettingsError):s.plan(self.path)
            self.assertEqual(self.path.read_text(), text)

    def test_readback_failure_rolls_back_and_blocks_restart(self):
        self.write({**s.DESIRED, 'BitTorr': self.defaults})
        backend = Backend(self.path, self.defaults);backend.bad_readback = True
        with self.assertRaisesRegex(s.SettingsError, 'prior effective settings restored'):
            s.apply(self.path, 'migrate', backend)
        self.assertEqual(backend.value, self.defaults)
        self.assertEqual(len(backend.sets), 2)
        self.assertTrue(s.pending(self.path).exists())
        with self.assertRaisesRegex(s.SettingsError, 'recovery marker'):s.plan(self.path)

    def test_failed_rollback_is_explicit_and_marker_is_kept(self):
        self.write({**s.DESIRED, 'BitTorr': self.defaults})
        backend = Backend(self.path, self.defaults)
        def fail(value):raise OSError('private backend detail')
        backend.set = fail
        with self.assertRaisesRegex(s.SettingsError, 'rollback unverified') as result:
            s.apply(self.path, 'migrate', backend)
        self.assertNotIn('private backend detail', str(result.exception))
        self.assertTrue(s.pending(self.path).exists())

    def test_backup_failure_prevents_forward_set(self):
        self.write({**s.DESIRED, 'BitTorr': self.defaults});backend = Backend(self.path, self.defaults)
        with patch.object(s, 'backup', side_effect=OSError('disk full')):
            with self.assertRaises(OSError):s.apply(self.path, 'migrate', backend)
        self.assertEqual(backend.sets, [])

    def test_types_and_backend_address_are_strict(self):
        self.assertFalse(s.matches({'a': 0}, {'a': False}))
        s.Client('http://home-media-verification-torrserver-4:8090')
        for url in ('https://example.invalid:8090', 'http://127.0.0.1:8090',
                    'http://home-media-verification-torrserver:8090/?secret=x',
                    'http://user@home-media-verification-torrserver:8090'):
            with self.assertRaises(s.SettingsError):s.Client(url)

    def test_readiness_is_bounded(self):
        class Missing:
            def get(self):raise OSError('offline')
        with patch.object(s.time, 'monotonic', side_effect=[0, 31]):
            with self.assertRaisesRegex(s.SettingsError, 'readiness timed out'):s.ready(Missing())


if __name__ == '__main__':unittest.main()
