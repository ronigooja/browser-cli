from contextlib import nullcontext
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
from unittest.mock import Mock, patch


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

    def mock_recovery_setup(self):
        chrome = Path.home() / "chrome"
        destination = Path.home() / "install"
        self.enterContext(patch.object(bridge, "CHROME", chrome))
        self.enterContext(patch.object(bridge, "INSTALL", destination))
        self.enterContext(patch.dict(os.environ, BROWSER_CLI_MCP="1"))
        profile = chrome / "Profile 5"
        profile.mkdir(parents=True)
        (chrome / "Local State").write_text(json.dumps({"profile": {"info_cache": {
            profile.name: {"name": "Work"},
        }}}))
        with patch.object(bridge.subprocess, "run"), patch.object(sys, "stdout", io.StringIO()):
            bridge.install(profile.name)
        identifier = bridge.extension_origin(destination / "extension").split("/")[2]
        preferences = json.dumps({"extensions": {"settings": {identifier: {"location": 4}}}})
        (profile / "Preferences").write_text(preferences)
        (profile / "Secure Preferences").write_text(preferences)  # Same profile, not ambiguous.
        return profile

    def test_mcp_wakes_zero_window_chrome_once_and_forwards_original_request(self):
        profile = self.mock_recovery_setup()
        client = Mock()
        with patch.object(bridge, "connected_client", side_effect=[nullcontext(None), nullcontext(None),
                                                                   nullcontext(None), nullcontext(client)]) as connect, \
                patch.object(bridge.subprocess, "run", side_effect=[Mock(stdout="0\n"), Mock()]) as run, \
                patch.object(bridge, "receive", return_value={"id": 42, "action": "created"}) as receive, \
                patch.object(bridge.time, "monotonic", return_value=5), \
                patch.object(bridge.time, "time", return_value=100), \
                patch.object(bridge.time, "sleep") as sleep, patch.object(sys, "stdout", io.StringIO()) as output:
            self.assertEqual(bridge.background_open("https://example.com/", 10), 0)
        self.assertEqual(output.getvalue(), "id:42\tcreated\n")
        query, wake = run.call_args_list
        self.assertEqual(query.args[0][0], "/usr/bin/osascript")
        self.assertIn("is not running", query.args[0][2])
        self.assertIn("count of windows", query.args[0][4])
        self.assertEqual(wake.args[0], ["/usr/bin/open", "-g", "-n", "-a", "Google Chrome", "--args",
                                       f"--profile-directory={profile.name}", "--no-startup-window"])
        self.assertEqual(wake.kwargs["timeout"], 10)
        self.assertEqual([call.args[0] for call in connect.call_args_list], [15, 15, 7, 7])
        sleep.assert_called_once_with(bridge.WAKE_POLL)
        client.sendall.assert_called_once_with(bridge.frame({"url": "https://example.com/", "expires": 110000}))
        receive.assert_called_once_with(client.fileno.return_value, 15)

    def test_mcp_does_not_wake_when_chrome_is_stopped_has_windows_or_count_is_unknown(self):
        self.mock_recovery_setup()
        for count, diagnostic in (("not-running", "not running"), ("1", "open window"), ("?", "Could not confirm")):
            with self.subTest(count=count), \
                    patch.object(bridge, "connected_client", return_value=nullcontext(None)), \
                    patch.object(bridge.subprocess, "run", return_value=Mock(stdout=count)) as run:
                with self.assertRaisesRegex(bridge.ConnectionRequired, diagnostic) as raised:
                    bridge.background_open("https://example.com/", 3)
                self.assertEqual(raised.exception.state, "loaded")
                run.assert_called_once()
                self.assertEqual(run.call_args.args[0][0], "/usr/bin/osascript")

    def test_mcp_does_not_wake_if_host_connects_during_preflight(self):
        self.mock_recovery_setup()
        client = Mock()
        with patch.object(bridge, "connected_client", side_effect=[nullcontext(None), nullcontext(client)]), \
                patch.object(bridge.subprocess, "run", return_value=Mock(stdout="0")) as run, \
                patch.object(bridge, "deliver_background_request", return_value=0) as deliver:
            self.assertEqual(bridge.background_open("https://example.com/", 3), 0)
        run.assert_called_once()
        self.assertEqual(run.call_args.args[0][0], "/usr/bin/osascript")
        deliver.assert_called_once()
        self.assertIs(deliver.call_args.args[0], client)

    def test_mcp_refuses_ambiguous_or_undiscoverable_profile_and_bad_registration(self):
        profile = self.mock_recovery_setup()
        with patch.object(bridge, "connected_client", return_value=nullcontext(None)), \
                patch.object(bridge.subprocess, "run") as run:
            other = profile.with_name("Profile 6")
            other.mkdir()
            other_preferences = other / "Preferences"
            other_preferences.write_bytes((profile / "Preferences").read_bytes())
            with self.assertRaisesRegex(bridge.ConnectionRequired, "more than one Chrome profile"):
                bridge.background_open("https://example.com/", 3)
            other_preferences.unlink()
            with patch.object(bridge, "discover_profiles", return_value={}):
                with self.assertRaisesRegex(bridge.ConnectionRequired, "discoverable regular"):
                    bridge.background_open("https://example.com/", 3)
            registration = bridge.host_manifest()
            data = json.loads(registration.read_bytes())
            registration.write_text(json.dumps(dict(data, path="/some/other/native-host")))
            with self.assertRaisesRegex(bridge.ConnectionRequired, "native host registration"):
                bridge.background_open("https://example.com/", 3)
            run.assert_not_called()

    def test_mcp_wait_for_reconnect_does_not_extend_original_deadline(self):
        self.mock_recovery_setup()
        now = [0.0]

        def advance(seconds):
            now[0] += seconds

        with patch.object(bridge.time, "monotonic", side_effect=lambda: now[0]), \
                patch.object(bridge.time, "sleep", side_effect=advance), \
                patch.object(bridge, "connected_client", return_value=nullcontext(None)) as connect, \
                patch.object(bridge.subprocess, "run", side_effect=[Mock(stdout="0"), Mock()]) as run, \
                patch.object(sys, "argv", ["bridge.py", "open", "https://example.com/", "1"]), \
                patch.object(sys, "stderr", io.StringIO()) as error:
            self.assertEqual(bridge.main(), 22)
        self.assertIn("did not reconnect the native host in time. The URL was not sent", error.getvalue())
        self.assertEqual(now[0], 1)
        self.assertEqual(run.call_count, 2)
        self.assertTrue(all(call.args == (1,) for call in connect.call_args_list))

    def test_mcp_wake_failure_does_not_retry_or_forward(self):
        self.mock_recovery_setup()
        for failure in (subprocess.TimeoutExpired("open", 1), subprocess.CalledProcessError(1, "open")):
            with self.subTest(failure=failure), \
                    patch.object(bridge, "connected_client", return_value=nullcontext(None)) as connect, \
                    patch.object(bridge.subprocess, "run", side_effect=[Mock(stdout="0"), failure]) as run, \
                    patch.object(bridge, "deliver_background_request") as deliver:
                with self.assertRaisesRegex(bridge.ConnectionRequired, "The URL was not sent"):
                    bridge.background_open("https://example.com/", 3)
                self.assertEqual(run.call_count, 2)
                self.assertEqual(connect.call_count, 2)
                deliver.assert_not_called()

    def test_cli_open_does_not_attempt_wake(self):
        self.mock_recovery_setup()
        with patch.dict(os.environ, BROWSER_CLI_MCP="0"), \
                patch.object(bridge, "connected_client", return_value=nullcontext(None)), \
                patch.object(bridge.subprocess, "run") as run:
            with self.assertRaisesRegex(bridge.ConnectionRequired, "installed but not connected"):
                bridge.background_open("https://example.com/", 3)
            run.assert_not_called()

    def test_mcp_never_wakes_or_resends_after_delivery_failure(self):
        client = Mock()
        for after_wake in (False, True):
            with self.subTest(after_wake=after_wake), patch.dict(os.environ, BROWSER_CLI_MCP="1"), \
                    patch.object(bridge, "connected_client", side_effect=(
                        [nullcontext(None), nullcontext(None)] if after_wake else []
                    ) + [nullcontext(client)]) as connect, \
                    patch.object(bridge, "recovery_profile", return_value="Default") as profile, \
                    patch.object(bridge.subprocess, "run", return_value=Mock(stdout="0")) as run, \
                    patch.object(bridge, "receive", side_effect=EOFError):
                client.reset_mock()
                with self.assertRaisesRegex(ValueError, "a tab may already have opened"):
                    bridge.background_open("https://example.com/", 3)
                client.sendall.assert_called_once()
                self.assertEqual(connect.call_count, 3 if after_wake else 1)
                self.assertEqual(run.call_count, 2 if after_wake else 0)
                self.assertEqual(profile.call_count, int(after_wake))

    def test_mcp_check_remains_observation_only_when_disconnected(self):
        self.mock_recovery_setup()
        with patch.object(bridge, "connected_client", return_value=nullcontext(None)) as connect, \
                patch.object(bridge.subprocess, "run") as run, \
                patch.object(sys, "argv", ["bridge.py", "check"]), \
                patch.object(sys, "stderr", io.StringIO()) as error:
            self.assertEqual(bridge.main(), 22)
        self.assertIn("installed but not connected", error.getvalue())
        connect.assert_called_once()
        run.assert_not_called()

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
                patch.object(sys, "stdout", io.StringIO()) as output, patch.object(bridge.subprocess, "run") as launch, \
                patch.object(sys.stdin, "isatty", return_value=True), \
                patch.object(sys.stderr, "isatty", return_value=True), patch.object(bridge, "check_setup") as check:
            bridge.install()
        check.assert_not_called()
        self.assertIn("--profile-directory=Profile 5", launch.call_args.args[0])
        self.assertIn("Finish setup in Chrome profile 'Work'", output.getvalue())
        self.assertIn("Command+Shift+G", output.getvalue())
        self.assertIn("Click Select to load the entire folder", output.getvalue())
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
                with self.assertRaisesRegex(ValueError, "Ask the user which profile"):
                    bridge.choose_profile()
                self.assertEqual(bridge.choose_profile("Profile 2"), ("Profile 2", "'Work'"))
                with self.assertRaisesRegex(ValueError, "Unknown Chrome profile"):
                    bridge.choose_profile("../Profile 2")
            with patch.object(sys.stdin, "isatty", return_value=True), \
                    patch.object(sys.stderr, "isatty", return_value=True), patch("builtins.input", return_value="2"):
                self.assertEqual(bridge.choose_profile(), ("Profile 2", "'Work'"))

    def test_uninstall_requires_removal_in_chrome_then_only_cleans_known_files(self):
        chrome = Path.home() / "chrome"
        destination = Path.home() / "install"
        with patch.object(bridge, "CHROME", chrome), patch.object(bridge, "INSTALL", destination), \
                patch.object(bridge.subprocess, "run"), patch.object(sys, "stdout", io.StringIO()):
            bridge.install()
            origin = bridge.extension_origin(destination / "extension")
            profile = chrome / "Default"
            profile.mkdir()
            preferences = profile / "Preferences"
            preferences.write_text(json.dumps({"extensions": {"settings": {origin.split("/")[2]: {}}}}))
            with self.assertRaisesRegex(ValueError, "Remove browser-cli Background Tabs"):
                bridge.uninstall()
            self.assertTrue(bridge.host_manifest().is_file())
            preferences.write_text("{}")
            unrelated = destination / "keep.txt"
            unrelated.touch()
            bridge.uninstall()
            bridge.uninstall()
            self.assertFalse(bridge.host_manifest().exists())
            self.assertEqual(list(destination.iterdir()), [unrelated])
            self.assertEqual(preferences.read_text(), "{}")


if __name__ == "__main__":
    unittest.main()
