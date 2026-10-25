"""Ensure public exports reject private tailnet endpoints, including future ones."""
import importlib.util
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

spec=importlib.util.spec_from_file_location('project_export',Path(__file__).with_name('export_project.py'))
exporter=importlib.util.module_from_spec(spec)
spec.loader.exec_module(exporter)


class PrivacyTests(unittest.TestCase):
    def test_missing_private_profile_cannot_create_a_public_export(self):
        with tempfile.TemporaryDirectory() as directory,patch.object(exporter,'private_replacements',return_value={}):
            destination=Path(directory)/'public'
            with self.assertRaises(RuntimeError):exporter.export(destination)
            self.assertFalse(destination.exists())

    def test_future_tailnet_endpoints_are_rejected_without_disclosing_values(self):
        endpoints=[
            '.'.join(map(str,(100,64,1,2))),
            '.'.join(map(str,(100,127,254,254))),
            '.'.join(map(str,(10,1,2,3))),
            '.'.join(map(str,(192,168,2,3))),
            '.'.join(map(str,(172,20,2,3))),
            'server.'+'tail-example.'+'ts.'+'net',
            ':'.join(format(i,'x') for i in (0xfd7a,0x115c,0xa1e0,0,0,0,0,7)),
        ]
        with tempfile.TemporaryDirectory() as directory,patch.object(exporter,'secret_values',return_value=set()):
            root=Path(directory)
            for endpoint in endpoints:
                (root/'README.md').write_text('Endpoint: '+endpoint)
                with self.assertRaises(RuntimeError) as caught:exporter.verify(root)
                self.assertNotIn(endpoint,str(caught.exception))

    def test_placeholders_remain_exportable(self):
        with tempfile.TemporaryDirectory() as directory,patch.object(exporter,'secret_values',return_value=set()):
            root=Path(directory)
            (root/'README.md').write_text('http://<TAILSCALE_IP>/ <TAILSCALE_HOSTNAME> <SERVER_LAN_IP>')
            exporter.verify(root)

    def test_export_templates_private_identifiers_without_modifying_live_files(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            (root/'config.py').write_text("ROOT='private-device-identifier'")
            with patch.object(exporter,'private_replacements',return_value={'STORAGE_ID':'private-device-identifier'}):
                exporter.sanitize_snapshot(root)
            self.assertEqual((root/'config.py').read_text(),"ROOT='<STORAGE_ID>'")
            self.assertNotIn('private-device-identifier',(root/'deployment-values.example.json').read_text())

    def test_opaque_compiled_artifact_is_not_exportable(self):
        with tempfile.TemporaryDirectory() as directory,patch.object(exporter,'secret_values',return_value=set()):
            root=Path(directory);(root/'plugin.dll').write_bytes(b'opaque')
            with self.assertRaises(RuntimeError):exporter.verify(root)

    def test_software_versions_are_not_mistaken_for_network_addresses(self):
        with tempfile.TemporaryDirectory() as directory,patch.object(exporter,'secret_values',return_value=set()):
            root=Path(directory)
            (root/'meta.json').write_text('{"targetAbi":"10.11.11.0","version":"1.4.0.12"}')
            exporter.verify(root)

    def test_private_rendering_can_complete_in_stages(self):
        import json
        spec=importlib.util.spec_from_file_location('deployment_renderer',Path(__file__).with_name('render_deployment.py'))
        renderer=importlib.util.module_from_spec(spec);spec.loader.exec_module(renderer)
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            (root/'deployment-values.example.json').write_text(json.dumps({'HOST':'','LIBRARY_ID':''}))
            (root/'config.py').write_text("HOST='<HOST>'\nLIBRARY_ID='<LIBRARY_ID>'\n")
            _,remaining=renderer.render(root,{'HOST':'example.invalid'})
            self.assertEqual(remaining,['LIBRARY_ID'])
            renderer.render(root,{'LIBRARY_ID':'1'*32},strict=True)
            compile((root/'config.py').read_text(),'config.py','exec')


if __name__=='__main__':unittest.main()
