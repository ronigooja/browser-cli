import os
from pathlib import Path
import subprocess
import sys
import unittest
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from agent_browser_mcp_setup import GRANT_SECONDS, Setup, TOOL, load_bridge


CLI = "/example/agent-browser"
PREFIX = [CLI, "--browser", "chrome", "install-background"]
SCOPE = object()


class SetupTests(unittest.TestCase):
    def setUp(self):
        self.setup = Setup(CLI)
        self.bridge = MagicMock()
        self.bridge.discover_profiles.return_value = {"Default": "Personal"}
        self.bridge.loading_instructions.return_value = (
            "Developer mode, Load unpacked, Command+Shift+G, Return, greyed out, Click Select, Reload"
        )
        self.load_bridge = self.enter_patch("agent_browser_mcp_setup.load_bridge", return_value=self.bridge)
        self.run = self.enter_patch("agent_browser_mcp_setup.subprocess.run")
        self.run.return_value.returncode = 20

    def enter_patch(self, target, **options):
        patcher = patch(target, **options)
        self.addCleanup(patcher.stop)
        return patcher.start()

    def prepare(self, **kwargs):
        return self.setup.call({"action": "prepare", **kwargs}, SCOPE)

    def install(self, grant, scope=SCOPE, **kwargs):
        return self.setup.call({"action": "install", "grant": grant, **kwargs}, scope)

    def commands(self):
        return [entry.args[0] for entry in self.run.call_args_list]

    def test_no_discovery_or_installer_without_explicit_consent(self):
        self.assertEqual(TOOL["name"], "setup_chrome_background")
        for args in ({}, {"approved": False}):
            result = self.prepare(**args)
            self.assertEqual(result["status"], "consent_required")
            self.assertNotIn("grant", result)
        with self.assertRaises(ValueError):
            self.prepare(approved=1)
        self.assertEqual(self.install("g" * 32)["status"], "grant_required")
        self.load_bridge.assert_not_called()
        self.run.assert_not_called()

    def test_profile_choice_and_fixed_invocation(self):
        profiles = {"Default": "Personal", "Profile 4": "Work"}
        self.bridge.discover_profiles.return_value = profiles
        result = self.prepare(approved=True)
        self.assertEqual(result["status"], "profile_required")
        self.assertEqual(result["profiles"], [{"directory": key, "name": name} for key, name in profiles.items()])
        grant = result["grant"]
        self.assertEqual(self.install(grant)["status"], "profile_required")
        self.assertEqual(self.install(grant, profile="Profile 1")["status"], "profile_invalid")
        for invalid in ("--check", "../Default", "https://example.com/", "Default\n"):
            with self.assertRaises(ValueError):
                self.install(grant, profile=invalid)
        self.assertEqual(self.commands(), [PREFIX + ["--check"]])
        self.run.side_effect = [MagicMock(returncode=20), MagicMock(returncode=0, stdout=b"untrusted output")]
        result = self.install(grant, profile="Profile 4")
        self.assertEqual(result["status"], "extension_pending")
        self.assertEqual(result["next_action"], "check")
        self.assertEqual(result["profile"], {"directory": "Profile 4", "name": "Work"})
        self.assertIn("Command+Shift+G", result["instructions"])
        self.assertIn("greyed out", result["instructions"])
        self.assertIn("Click Select", result["instructions"])
        self.assertNotIn("untrusted output", str(result))
        self.assertEqual(self.commands(), [PREFIX + ["--check"]] * 2 + [PREFIX + ["--profile", "Profile 4"]])
        launch = self.run.call_args.kwargs
        self.assertEqual(launch["env"], dict(os.environ, AGENT_BROWSER_MCP="1"))
        self.assertEqual([launch[stream] for stream in ("stdin", "stdout", "stderr")], [subprocess.DEVNULL] * 3)
        self.assertNotIn("shell", launch)
        self.assertEqual(self.install(grant, profile="Profile 4")["status"], "grant_required")
        self.assertEqual(len(self.commands()), 3)

    def test_grants_expire_and_cannot_cross_scopes(self):
        clock = self.enter_patch("agent_browser_mcp_setup.time.monotonic", return_value=100)
        first = self.prepare(approved=True)["grant"]
        self.assertEqual(self.install(first, scope=object())["status"], "grant_required")
        self.assertEqual(self.install(first, scope=None)["status"], "grant_required")
        clock.return_value += GRANT_SECONDS
        self.assertEqual(self.install(first)["status"], "grant_required")
        self.assertEqual(self.commands(), [PREFIX + ["--check"]])

    def test_grant_must_still_be_valid_after_preflight(self):
        clock = self.enter_patch("agent_browser_mcp_setup.time.monotonic", return_value=100)
        grant = self.prepare(approved=True)["grant"]

        def expire_during_check(*args, **kwargs):
            clock.return_value += GRANT_SECONDS
            return MagicMock(returncode=20)

        self.run.side_effect = expire_during_check
        self.assertEqual(self.install(grant)["status"], "grant_required")
        self.assertEqual(self.commands(), [PREFIX + ["--check"]] * 2)

    def test_new_consent_flow_revokes_previous_grant(self):
        first = self.prepare(approved=True)["grant"]
        second = self.prepare(approved=True)["grant"]
        self.assertEqual(self.install(first)["status"], "grant_required")
        self.assertEqual(self.prepare(approved=False)["status"], "consent_required")
        self.assertEqual(self.install(second)["status"], "grant_required")
        self.assertEqual(self.commands(), [PREFIX + ["--check"]] * 2)

    def test_install_failure_consumes_grant_and_requires_check(self):
        for failure, status in ((MagicMock(returncode=1), "setup_error"),
                                (subprocess.TimeoutExpired(PREFIX, 20), "setup_timeout")):
            self.run.side_effect = None
            grant = self.prepare(approved=True)["grant"]
            self.run.side_effect = [MagicMock(returncode=20), failure]
            result = self.install(grant)
            self.assertEqual(result["status"], status)
            self.assertEqual(result["next_action"], "check")
            self.assertEqual(self.install(grant)["status"], "grant_required")

    def test_profile_changes_prevent_installation(self):
        grant = self.prepare(approved=True)["grant"]
        self.bridge.discover_profiles.return_value = {"Profile 1": "Work"}
        self.assertEqual(self.install(grant)["status"], "profiles_changed")
        self.assertEqual(self.commands(), [PREFIX + ["--check"]] * 2)

    def test_invalid_discovery_cannot_turn_multiple_profiles_into_one(self):
        self.bridge.discover_profiles.return_value = {"Default": "Personal", "--profile": "Invalid"}
        result = self.prepare(approved=True)
        self.assertEqual(result["status"], "setup_error")
        self.assertNotIn("grant", result)
        self.assertEqual(self.commands(), [PREFIX + ["--check"]])

    def test_fresh_chrome_default_uses_explicit_profile(self):
        self.bridge.discover_profiles.return_value = {}
        result = self.prepare(approved=True)
        self.assertEqual(result["status"], "install_ready")
        self.assertEqual(result["profiles"], [{"directory": "Default", "name": "Default"}])
        self.run.side_effect = [MagicMock(returncode=20), MagicMock(returncode=0)]
        self.assertEqual(self.install(result["grant"])["status"], "extension_pending")
        self.assertEqual(self.commands()[-1], PREFIX + ["--profile", "Default"])

    def test_pending_and_disconnected_setup_only_check(self):
        for code, status in ((0, "extension_connected"), (21, "extension_pending"),
                             (22, "extension_disconnected"), (1, "setup_error")):
            with self.subTest(code=code):
                self.run.reset_mock()
                self.run.side_effect = [MagicMock(returncode=20), *(MagicMock(returncode=code) for _ in range(3))]
                grant = self.prepare(approved=True)["grant"]
                for response in (self.install(grant), self.prepare(approved=True),
                                 self.setup.call({"action": "check"}, SCOPE)):
                    self.assertEqual(response["status"], status)
                    self.assertNotIn("grant", response)
                self.assertEqual(self.commands(), [PREFIX + ["--check"]] * 4)

    def test_additional_arguments_cannot_retarget_setup(self):
        for action in ("check", "prepare", "install"):
            with self.subTest(action=action), self.assertRaises(ValueError):
                self.setup.call({"action": action, "url": "https://example.com/"}, SCOPE)
        self.run.assert_not_called()


class BridgeDiscoveryTests(unittest.TestCase):
    def setUp(self):
        self.bridge = load_bridge(Path(__file__).resolve().parents[1] / "agent-browser")

    def test_connection_check_does_not_create_a_runtime_directory(self):
        with patch.object(self.bridge, "runtime_dir", side_effect=FileNotFoundError) as directory:
            self.assertFalse(self.bridge.check_setup())
        directory.assert_called_once_with(create=False)

    def test_empty_preference_entries_do_not_block_mcp_setup(self):
        bridge = self.bridge
        chrome = MagicMock()
        preference = MagicMock()
        preference.parent.name = "Default"
        preference.read_text.return_value = '{"extensions":{"settings":{"abc":{}}}}'
        chrome.glob.side_effect = [[preference], []]
        with patch.object(bridge, "CHROME", chrome), patch.object(bridge, "host_manifest") as host, \
                patch.object(bridge, "extension_origin", return_value="chrome-extension://abc/"), \
                patch.dict(os.environ, AGENT_BROWSER_MCP="1"):
            host.return_value.is_file.return_value = False
            self.assertEqual(bridge.setup_state(), "missing")
            chrome.glob.side_effect = [[preference], []]
            self.assertEqual(list(bridge.loaded_profiles("chrome-extension://abc/", strict=True)), ["Default"])
            chrome.glob.side_effect = [[preference], []]
            preference.read_text.return_value = '{"extensions":{"settings":{"abc":{"path":"extension"}}}}'
            self.assertEqual(bridge.setup_state(), "loaded")
