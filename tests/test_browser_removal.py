import json
import os
from pathlib import Path
import shutil
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


if __name__ == "__main__":
    unittest.main()
