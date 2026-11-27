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
import threading
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

    def production_round_trip(self, host, operation, payload, replies):
        observed = []

        def extension():
            message = bridge.receive(host.stdout.fileno(), time.monotonic() + 3)
            observed.append(message)
            for reply in replies(message):
                host.stdin.write(reply)
                host.stdin.flush()

        thread = threading.Thread(target=extension)
        thread.start()
        try:
            return bridge.request(operation, payload, 3), observed
        finally:
            thread.join(3)
            self.assertFalse(thread.is_alive())

    def test_native_versioned_round_trip(self):
        host = self.start_host()
        self.wait_for_hosts(1)
        response, observed = self.production_round_trip(host, "find", {"url": "https://example.com"},
            lambda req: [bridge.frame({"v": bridge.PROTOCOL, "id": req["id"], "status": "ok", "tabs": []})])
        self.assertEqual(response, {"status": "ok", "tabs": []})
        self.assertEqual(len(observed), 1)
        self.assertEqual(observed[0]["operation"], "find")
        self.assertEqual(observed[0]["payload"], {"url": "https://example.com"})
        self.assertNotIn("url", observed[0])  # A legacy receiver can't perform a write.

    def test_native_rejects_unversioned_open(self):
        with self.assertRaisesRegex(ValueError, "Invalid Chrome background request"):
            bridge.native_request({"url": "https://example.com/", "expires": int((time.time() + 3) * 1000)})

    def test_reencoding_an_oversize_response_does_not_disconnect(self):
        host = self.start_host()
        self.wait_for_hosts(1)

        def oversized(req):
            # Native UTF-8 fits but ASCII-escaped JSON is too large.
            content = json.dumps({"v": bridge.PROTOCOL, "id": req["id"], "status": "ok", "text": "界" * 12000}, ensure_ascii=False).encode()
            return [bridge.struct.pack("=I", len(content)) + content]

        response, _ = self.production_round_trip(host, "read", {}, oversized)
        self.assertEqual(response, {"status": "response_too_large"})
        response, _ = self.production_round_trip(host, "status", None,
            lambda req: [bridge.frame({"v": bridge.PROTOCOL, "id": req["id"], "status": "ok"})])
        self.assertEqual(response, {"status": "ok"})

    def test_oversize_native_frame_replies_before_retiring_connection(self):
        host = self.start_host()
        self.wait_for_hosts(1)
        response, _ = self.production_round_trip(host, "open", {"url": "https://example.com"},
            lambda req: [bridge.struct.pack("=I", bridge.LIMIT + 1)])
        self.assertEqual(response, {"status": "open_unconfirmed"})
        self.assertEqual(host.wait(3), 0)

    def test_legacy_extension_reply_reports_update_not_connected(self):
        host = self.start_host()
        self.wait_for_hosts(1)
        response, _ = self.production_round_trip(host, "status", None,
            lambda req: [bridge.frame({"error": "Chrome background open requires an HTTP(S) URL"})])
        self.assertEqual(response, {"status": "disconnected", "update_required": True})

    def test_live_prior_version_host_or_worker_reports_update_without_replay(self):
        host = self.start_host()
        self.wait_for_hosts(1)
        for operation, prior_status, expected in (
            ("status", "invalid_request", {"status": "disconnected", "update_required": True}),
            ("open", "protocol_mismatch", {"status": "disconnected", "update_required": True}),
            ("close", "protocol_mismatch", {"status": "cleanup_instance_changed"}),
        ):
            with self.subTest(operation=operation):
                response, observed = self.production_round_trip(host, operation, {},
                    lambda req: [bridge.frame({"v": 1, "id": req["id"], "status": prior_status})])
                self.assertEqual(response, expected)
                self.assertEqual([req["v"] for req in observed], [bridge.PROTOCOL])
                self.assertEqual(select.select([host.stdout], [], [], 0)[0], [])

    def test_request_does_not_retry_uncertain_writes(self):
        for operation, status in (("open", "open_unconfirmed"), ("close", "cleanup_unconfirmed")):
            with self.subTest(operation=operation):
                client = Mock()
                with patch.object(bridge, "connected_client", return_value=nullcontext(client)) as connect, \
                        patch.object(bridge, "receive", side_effect=EOFError):
                    self.assertEqual(bridge.request(operation, {}, 3), {"status": status})
                connect.assert_called_once()
                client.sendall.assert_called_once()

    def test_status_reports_missing_pending_disconnected_and_update_without_launch(self):
        with patch.object(bridge, "CHROME", Path.home() / "chrome"), \
                patch.object(bridge, "INSTALL", Path.home() / "install"), \
                patch.object(sys, "stdout", io.StringIO()):
            self.assertEqual(bridge.request("status", {}, 3), {"status": "extension_required"})
            bridge.install(open_settings=False)
            self.assertEqual(bridge.request("status", {}, 3), {"status": "pending"})
            origin = bridge.extension_origin(bridge.INSTALL / "extension")
            profile = bridge.CHROME / "Default"
            profile.mkdir()
            (profile / "Preferences").write_text(json.dumps({"extensions": {"settings": {origin.split("/")[2]: {"location": 4}}}}))
            self.assertEqual(bridge.request("status", {}, 3), {"status": "disconnected"})
            target = bridge.INSTALL / "extension/manifest.json"
            target.write_text(json.dumps(dict(json.loads(target.read_bytes()), version="2.0.0")))
            self.assertEqual(bridge.request("status", {}, 3), {"status": "disconnected", "update_required": True})
            bridge.install(open_settings=False)
            self.assertFalse(bridge.staged_update_required())
            self.assertEqual(bridge.request("status", {}, 3), {"status": "disconnected"})

    def test_installer_refuses_browser_launch(self):
        with self.assertRaisesRegex(ValueError, "Launching Chrome is not supported"):
            bridge.install(open_settings=True)

    def test_check_detects_connection_without_opening_a_tab(self):
        self.assertFalse(bridge.check_setup())
        host = self.start_host()
        self.wait_for_hosts(1)
        response, observed = self.production_round_trip(host, "status", {},
            lambda req: [bridge.frame({"v": bridge.PROTOCOL, "id": req["id"], "status": "ok"})])
        self.assertEqual(observed[0]["operation"], "status")
        with patch.object(sys, "stdout", io.StringIO()) as output, patch.object(bridge, "request", return_value=response):
            self.assertTrue(bridge.check_setup())
            self.assertIn("setup complete", output.getvalue())
        self.assertEqual(select.select([host.stdout], [], [], 0.1)[0], [])

    def test_multiple_profiles_do_not_send_a_native_request(self):
        hosts = [self.start_host(), self.start_host()]
        self.wait_for_hosts(2)
        self.assertEqual(bridge.request("status", None, 3), {"status": "ambiguous"})
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
                patch.object(sys, "stdout", io.StringIO()) as output, \
                patch.object(sys.stdin, "isatty", return_value=True), \
                patch.object(sys.stderr, "isatty", return_value=True), patch.object(bridge, "check_setup") as check:
            bridge.install()
        check.assert_not_called()
        self.assertIn("Finish setup in Chrome profile 'Work'", output.getvalue())
        self.assertIn("Command+Shift+G", output.getvalue())
        self.assertIn("Click Select to load the entire folder", output.getvalue())
        self.assertIn("Setup is pending", output.getvalue())
        registration = json.loads((chrome / "NativeMessagingHosts" / f"{bridge.HOST}.json").read_text())
        self.assertTrue(os.access(registration["path"], os.X_OK))
        self.assertEqual(registration["allowed_origins"], [bridge.extension_origin(destination / "extension")])
        staged = destination / "extension/read-page.js"
        self.assertFalse(staged.is_symlink())
        self.assertEqual(staged.read_bytes(), (SOURCE.parent / "extension/shared/read-page.js").read_bytes())
        with self.enterContext(subprocess.Popen([registration["path"], registration["allowed_origins"][0]],
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, env=self.env)) as host:
            self.wait_for_hosts(1)
            try:
                response, _ = self.production_round_trip(host, "status", None,
                    lambda req: [bridge.frame({"v": bridge.PROTOCOL, "id": req["id"], "status": "ok"})])
                self.assertEqual(response, {"status": "ok"})
            finally:
                self.stop_host(host)

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



if __name__ == "__main__":
    unittest.main()
