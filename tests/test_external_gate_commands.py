import pathlib
import sys
import unittest
from unittest.mock import patch

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tests"))
import check_plugin
import check_torrserver_patch


class ExternalGateCommandChecks(unittest.TestCase):
    def run_main(self, module, args):
        with patch.object(sys, "argv", [module.__file__, "--repository", str(ROOT)]), \
             patch.object(module.subprocess, "run", return_value=type("Result", (), {"returncode": 0})()) as run:
            self.assertEqual(module.main(), 0)
        return [call.args[0] for call in run.call_args_list]

    def test_plugin_gate_builds_only_disposable_check_target(self):
        commands = self.run_main(check_plugin, [])
        build = commands[0]
        self.assertEqual(build[:3], ["docker", "build", "--pull=false"])
        self.assertIn("checks", build)
        self.assertNotIn("compose", build)
        self.assertNotIn("run", build)
        self.assertEqual(commands[1][1:3], ["image", "rm"])

    def test_torrserver_gate_never_builds_or_runs_runtime_stage(self):
        commands = self.run_main(check_torrserver_patch, [])
        build = commands[0]
        self.assertEqual(build[:3], ["docker", "build", "--pull=false"])
        self.assertIn("builder", build)
        self.assertNotIn("compose", build)
        self.assertNotIn("run", build)
        self.assertEqual(commands[1][1:3], ["image", "rm"])

    def test_plugin_gate_uses_locked_package_versions_and_offline_tests(self):
        dockerfile = (ROOT / "plugin-source/dynamic-library/Gate.Dockerfile").read_text()
        self.assertIn("dotnet restore AudioChecks/AudioChecks.csproj --locked-mode", dockerfile)
        self.assertIn("RUN --network=none dotnet run", dockerfile)
        self.assertIn("192.0.2.18", dockerfile)

    def test_torrserver_tests_and_compile_run_without_network(self):
        dockerfile = (ROOT / "deploy/media-stack/torrserver/Dockerfile").read_text()
        self.assertIn("RUN --network=default go mod download", dockerfile)
        self.assertIn("RUN --network=none go test", dockerfile)
        self.assertIn("--target", (ROOT / "tests/check_torrserver_patch.py").read_text())


if __name__ == "__main__":
    unittest.main()
