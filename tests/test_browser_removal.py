import json
import os
from pathlib import Path
import plistlib
import shutil
import subprocess
import tempfile
import unittest
from unittest.mock import patch

import browser_removal as removal


class RemovalTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.home = Path(self.temp.name)
        (self.home / ".Trash").mkdir(mode=0o700)
        self.install = self.home / "Library/Application Support/agent-browser/chrome-background"
        self.install.mkdir(parents=True)
        shutil.copytree(removal.SOURCE / "chrome-background/extension", self.install / "extension")
        shutil.copyfile(removal.SOURCE / "extension/shared/read-page.js", self.install / "extension/read-page.js")
        shutil.copyfile(removal.SOURCE / "chrome-background/bridge.py", self.install / "bridge.py")
        import shlex
        (self.install / "native-host").write_text(f'#!/bin/sh\nexec /usr/bin/python3 -I {shlex.quote(str(self.install / "bridge.py"))} host "${{1-}}"\n')
        self.registration = self.home / "Library/Application Support/Google/Chrome/NativeMessagingHosts" / (removal.HOST + ".json")
        self.registration.parent.mkdir(parents=True)
        self.registration.write_text(json.dumps({"name": removal.HOST, "description": "agent-browser local Chrome extension transport", "path": str(self.install / "native-host"), "type": "stdio", "allowed_origins": [removal._origin()]}))
        self.home_patch = patch.object(Path, "home", return_value=self.home)
        self.home_patch.start()
        self.addCleanup(self.home_patch.stop)
        self.addCleanup(self.temp.cleanup)

    def test_moves_exact_files_reversibly(self):
        result = removal.cleanup("chrome")
        self.assertEqual(result["errors"], [])
        self.assertEqual(len(result["moved"]), 2)
        for move in result["moved"]:
            self.assertFalse(Path(move["from"]).exists())
            os.rename(move["to"], move["from"])
        self.assertTrue(self.install.exists())
        self.assertTrue(self.registration.exists())

    def test_unexpected_file_gives_partial_result(self):
        (self.install / "personal").write_text("keep")
        result = removal.cleanup("chrome")
        self.assertTrue(self.install.exists())
        self.assertEqual(len(result["moved"]), 1)
        self.assertTrue(result["errors"])

    def test_wrong_origin_retained(self):
        data = json.loads(self.registration.read_text())
        data["allowed_origins"] = ["chrome-extension://wrong/"]
        self.registration.write_text(json.dumps(data))
        result = removal.cleanup("chrome")
        self.assertTrue(self.registration.exists())
        self.assertEqual(len(result["moved"]), 1)
        self.assertTrue(result["errors"])

    def test_symlink_install_retained(self):
        (self.install / "bridge.py").unlink()
        (self.install / "bridge.py").symlink_to(removal.SOURCE / "chrome-background/bridge.py")
        result = removal.cleanup("chrome")
        self.assertTrue(self.install.exists())
        self.assertTrue(result["errors"])

    def test_profile_registration_vetoes_cleanup(self):
        profile = self.registration.parent.parent / "Default"
        profile.mkdir()
        identifier = removal._origin().split("/")[2]
        (profile / "Preferences").write_text(json.dumps({"extensions": {"settings": {identifier: {"path": "extension"}}}}))
        outcome = removal.cleanup("chrome")
        self.assertFalse(outcome["moved"])
        self.assertTrue(self.install.exists())
        self.assertTrue(self.registration.exists())

    def test_unsafe_trash_retains_both(self):
        (self.home / ".Trash").chmod(0o755)
        result = removal.cleanup("chrome")
        self.assertEqual(result["moved"], [])
        self.assertTrue(self.install.exists())
        self.assertTrue(self.registration.exists())

    def test_safari_never_moves(self):
        app = self.home / "Applications/AgentBrowserSafari.app"
        app.mkdir(parents=True)
        with patch.dict(os.environ, {"AGENT_BROWSER_SAFARI_BINARY": str(app)}):
            result = removal.cleanup("safari")
        self.assertTrue(app.exists())
        self.assertTrue(result["errors"])
        self.assertEqual(result["moved"], [])

    def make_safari(self):
        app = self.home / "Applications/AgentBrowserSafari.app"
        extension = app / "Contents/PlugIns/AgentBrowserSafariExtension.appex"
        for bundle, identifier in ((app, removal.SAFARI_HOST), (extension, removal.SAFARI_EXTENSION)):
            contents = bundle / "Contents"
            contents.mkdir(parents=True, exist_ok=True)
            (contents / "Info.plist").write_bytes(plistlib.dumps({"CFBundleIdentifier": identifier}))
        return app, extension

    def test_safari_companion_already_removed_needs_no_signature(self):
        with patch.dict(os.environ, {}, clear=True):
            result = removal.cleanup("safari")
        self.assertEqual(result, {"browser": "safari", "moved": [], "errors": []})

    def test_safari_trusted_team_moves_only_app(self):
        app, extension = self.make_safari()
        group = self.home / "Library/Group Containers/example"
        group.mkdir(parents=True)
        with patch.dict(os.environ, {"AGENT_BROWSER_SAFARI_TEAM_ID": "ABCDE12345"}, clear=True), patch.object(removal.subprocess, "run") as run:
            result = removal.cleanup("safari")
        self.assertEqual(result["errors"], [])
        self.assertEqual(len(result["moved"]), 1)
        self.assertFalse(app.exists())
        self.assertTrue(group.exists())
        # Two validation passes, each: one deep strict app check plus one non-deep
        # strict requirement check per bundle.
        self.assertEqual(run.call_count, 6)
        calls = [call.args[0] for call in run.call_args_list]
        for args in calls:
            self.assertIn("--strict", args)
        for index in (0, 3):
            self.assertIn("--deep", calls[index])
            self.assertEqual(calls[index][-1], str(app))
            self.assertFalse(any(arg.startswith("-R") for arg in calls[index]))
        for index, identifier in ((1, removal.SAFARI_HOST), (2, removal.SAFARI_EXTENSION),
                                  (4, removal.SAFARI_HOST), (5, removal.SAFARI_EXTENSION)):
            args = calls[index]
            self.assertNotIn("--deep", args)
            requirement_args = [arg for arg in args if arg.startswith("-R")]
            self.assertEqual(len(requirement_args), 1)
            requirement = requirement_args[0]
            self.assertTrue(requirement.startswith("-R=anchor apple generic and identifier"))
            self.assertIn(f'identifier "{identifier}"', requirement)
            self.assertIn('"ABCDE12345"', requirement)
            self.assertEqual(args[-1], str(app) if identifier == removal.SAFARI_HOST else str(extension))
        os.rename(result["moved"][0]["to"], app)
        self.assertTrue(extension.exists())

    def test_safari_requires_pin_and_valid_signatures(self):
        app, _ = self.make_safari()
        for team in ("", 'bad" or true'):
            with patch.dict(os.environ, {"AGENT_BROWSER_SAFARI_TEAM_ID": team}, clear=True), patch.object(removal.subprocess, "run") as run:
                self.assertTrue(removal.cleanup("safari")["errors"])
                run.assert_not_called()
        with patch.dict(os.environ, {"AGENT_BROWSER_SAFARI_TEAM_ID": "ABCDE12345"}, clear=True), patch.object(removal.subprocess, "run", side_effect=subprocess.CalledProcessError(1, "codesign")):
            self.assertTrue(removal.cleanup("safari")["errors"])
        self.assertTrue(app.exists())

    def test_safari_wrong_identifier_and_symlink_rejected(self):
        app, extension = self.make_safari()
        plist = extension / "Contents/Info.plist"
        plist.write_bytes(plistlib.dumps({"CFBundleIdentifier": "wrong"}))
        with patch.dict(os.environ, {"AGENT_BROWSER_SAFARI_TEAM_ID": "ABCDE12345"}, clear=True), patch.object(removal.subprocess, "run"):
            self.assertTrue(removal.cleanup("safari")["errors"])
            plist.unlink()
            plist.symlink_to(app / "Contents/Info.plist")
            self.assertTrue(removal.cleanup("safari")["errors"])
        self.assertTrue(app.exists())


if __name__ == "__main__":
    unittest.main()
