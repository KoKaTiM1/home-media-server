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
    def test_future_tailnet_endpoints_are_rejected_without_disclosing_values(self):
        endpoints=[
            '.'.join(map(str,(100,64,1,2))),
            '.'.join(map(str,(100,127,254,254))),
            'server.'+'tail-example.'+'ts.'+'net',
            ':'.join(format(i,'x') for i in (0xfd7a,0x115c,0xa1e0,0,0,0,0,7)),
        ]
        with tempfile.TemporaryDirectory() as directory,patch.object(exporter,'secret_values',return_value=set()):
            root=Path(directory)
            for endpoint in endpoints:
                (root/'README.md').write_text('Endpoint: '+endpoint)
                with self.assertRaises(RuntimeError) as caught:exporter.verify(root)
                self.assertNotIn(endpoint,str(caught.exception))

    def test_placeholders_and_local_setup_reference_remain_exportable(self):
        with tempfile.TemporaryDirectory() as directory,patch.object(exporter,'secret_values',return_value=set()):
            root=Path(directory)
            (root/'README.md').write_text('http://<TAILSCALE_IP>/ <TAILSCALE_HOSTNAME> LAN: <SERVER_LAN_IP>')
            exporter.verify(root)


if __name__=='__main__':unittest.main()
