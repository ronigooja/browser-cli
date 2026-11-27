import importlib.util
import io
import json
import os
from pathlib import Path
import select
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch


SOURCE = Path(__file__).resolve().parents[1] / "chrome-background"
spec = importlib.util.spec_from_file_location("bridge", SOURCE / "bridge.py")
bridge = importlib.util.module_from_spec(spec)
spec.loader.exec_module(bridge)


class BackgroundTest(unittest.TestCase):
    def setUp(self):
        home = self.enterContext(tempfile.TemporaryDirectory(dir="/tmp"))
        self.env = dict(os.environ, HOME=home, PYTHONNOUSERSITE="1")
        self.runtime = self.enterContext(patch.object(Path, "home", return_value=Path(home)))
        self.addCleanup(self.clean_runtime)

    def clean_runtime(self):
        directory = bridge.runtime_dir()
        directory.rmdir()

    def start_host(self):
        origin = bridge.extension_origin(SOURCE / "extension")
        process = self.enterContext(subprocess.Popen(
            [sys.executable, str(SOURCE / "bridge.py"), "host", origin],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, env=self.env,
        ))
        self.addCleanup(self.stop_host, process)
        return process

    def stop_host(self, process):
        process.stdin.close()
        try:
            process.wait(timeout=3)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()

    def wait_for_hosts(self, count):
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            if len(list(bridge.runtime_dir().glob("host-*/socket"))) == count:
                return
            time.sleep(0.02)
        self.fail("Native host did not bind its socket")

    def start_client(self):
        client = self.enterContext(subprocess.Popen(
            [sys.executable, str(SOURCE / "bridge.py"), "open", "https://example.com/", "3"],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=self.env, text=True,
        ))
        self.addCleanup(client.kill)
        return client

    def test_native_round_trip(self):
        host = self.start_host()
        self.wait_for_hosts(1)
        client = self.start_client()
        request = bridge.receive(host.stdout.fileno(), time.monotonic() + 3)
        self.assertEqual(request["url"], "https://example.com/")
        host.stdin.write(bridge.frame({"id": 42, "action": "created"}))
        host.stdin.flush()
        self.assertEqual(client.communicate(timeout=3), ("id:42\tcreated\n", ""))
        self.assertEqual(client.returncode, 0)

    def test_missing_extension(self):
        client = self.start_client()
        output, error = client.communicate(timeout=3)
        self.assertEqual(output, "")
        self.assertIn("install-background", error)
        self.assertIn("Only after they confirm", error)
        self.assertEqual(client.returncode, 1)

    def test_interactive_setup_requires_explicit_consent(self):
        with patch.object(bridge, "setup_state", return_value="missing"), \
                patch.object(sys.stdin, "isatty", return_value=True), \
                patch.object(sys.stderr, "isatty", return_value=True), \
                patch.object(bridge, "install") as install:
            for answer in ("", "n", "maybe"):
                with self.subTest(answer=answer), patch("builtins.input", return_value=answer):
                    with self.assertRaisesRegex(ValueError, "no changes made"):
                        bridge.offer_setup()
            with patch("builtins.input", side_effect=EOFError):
                with self.assertRaisesRegex(ValueError, "no changes made"):
                    bridge.offer_setup()
            install.assert_not_called()
            with patch("builtins.input", return_value=" Y "):
                self.assertEqual(bridge.offer_setup(), 1)  # Original open did not happen.
            install.assert_called_once_with()

    def test_pending_and_disconnected_do_not_reinstall(self):
        chrome = Path.home() / "chrome"
        install_dir = Path.home() / "install"
        with patch.object(bridge, "CHROME", chrome), patch.object(bridge, "INSTALL", install_dir), \
                patch.object(bridge, "install") as install:
            manifest = bridge.host_manifest()
            manifest.parent.mkdir(parents=True)
            manifest.touch()
            extension = install_dir / "extension"
            extension.mkdir(parents=True)
            (extension / "manifest.json").touch()
            with self.assertRaisesRegex(ValueError, "setup is not complete"):
                bridge.offer_setup()
            profile = chrome / "Default"
            profile.mkdir()
            identifier = bridge.extension_origin(SOURCE / "extension").split("/")[2]
            (profile / "Secure Preferences").write_text(json.dumps({"extensions": {"settings": {identifier: {}}}}))
            with self.assertRaisesRegex(ValueError, "installed but not connected"):
                bridge.offer_setup()
            install.assert_not_called()

    def test_check_detects_connection_without_opening_a_tab(self):
        self.assertFalse(bridge.check_setup())
        host = self.start_host()
        self.wait_for_hosts(1)
        with patch.object(sys, "stdout", io.StringIO()) as output:
            self.assertTrue(bridge.check_setup())
        self.assertIn("setup complete", output.getvalue())
        self.assertEqual(select.select([host.stdout], [], [], 0.1)[0], [])

    def test_multiple_profiles_do_not_send_a_native_request(self):
        hosts = [self.start_host(), self.start_host()]
        self.wait_for_hosts(2)
        client = self.start_client()
        output, error = client.communicate(timeout=3)
        self.assertEqual(output, "")
        self.assertIn("more than one Chrome profile", error)
        self.assertEqual(client.returncode, 1)
        self.assertEqual(select.select([host.stdout for host in hosts], [], [], 0.1)[0], [])

    def test_installer_stages_files_for_single_profile(self):
        home = Path.home()
        chrome = home / "chrome"
        (chrome / "Profile 5").mkdir(parents=True)
        (chrome / "Local State").write_text(json.dumps({"profile": {"info_cache": {
            "Profile 5": {"name": "Work"}, "Profile 6": {"name": "Absent"},
        }}}))
        destination = home / "install"
        with patch.object(bridge, "CHROME", chrome), patch.object(bridge, "INSTALL", destination), \
                patch.object(sys, "stdout", io.StringIO()) as output, patch.object(bridge.subprocess, "run") as launch:
            bridge.install()
        self.assertIn("--profile-directory=Profile 5", launch.call_args.args[0])
        self.assertIn("Finish setup in Chrome profile 'Work'", output.getvalue())
        self.assertIn("Setup is pending", output.getvalue())
        registration = json.loads((chrome / "NativeMessagingHosts" / f"{bridge.HOST}.json").read_text())
        self.assertTrue(os.access(registration["path"], os.X_OK))
        self.assertEqual(registration["allowed_origins"], [bridge.extension_origin(destination / "extension")])

    def test_multiple_profiles_require_selection(self):
        chrome = Path.home() / "chrome"
        for name in ("Default", "Profile 2"):
            (chrome / name).mkdir(parents=True)
        (chrome / "Local State").write_text(json.dumps({"profile": {"info_cache": {
            "Default": {"name": "Personal"}, "Profile 2": {"name": "Work"},
        }}}))
        with patch.object(bridge, "CHROME", chrome), patch.object(sys, "stderr", io.StringIO()):
            with patch.object(sys.stdin, "isatty", return_value=False):
                with self.assertRaisesRegex(ValueError, "interactive terminal"):
                    bridge.choose_profile()
            with patch.object(sys.stdin, "isatty", return_value=True), patch("builtins.input", return_value="2"):
                self.assertEqual(bridge.choose_profile(), ("Profile 2", "'Work'"))


if __name__ == "__main__":
    unittest.main()
