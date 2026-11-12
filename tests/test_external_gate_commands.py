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

    def test_torrserver_gate_uses_disposable_builder_and_effective_settings_check(self):
        with patch.object(check_torrserver_patch, "check_settings") as effective:
            commands = self.run_main(check_torrserver_patch, [])
        effective.assert_called_once()
        self.assertEqual(effective.call_args.args[0], ROOT)
        self.assertEqual(effective.call_args.kwargs, {"entrypoint": "/tmp/torrserver"})
        build = commands[0]
        self.assertEqual(build[:3], ["docker", "build", "--pull=false"])
        self.assertIn("builder", build)
        self.assertNotIn("compose", build)
        self.assertNotIn("run", build)
        self.assertEqual(commands[1][1:3], ["image", "rm"])

    def test_failed_torrserver_build_does_not_run_effective_settings_check(self):
        with patch.object(sys, "argv", [check_torrserver_patch.__file__, "--repository", str(ROOT)]), \
             patch.object(check_torrserver_patch.subprocess, "run", return_value=type("Result", (), {"returncode": 1})()), \
             patch.object(check_torrserver_patch, "check_settings") as effective:
            self.assertEqual(check_torrserver_patch.main(), 1)
            effective.assert_not_called()

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
