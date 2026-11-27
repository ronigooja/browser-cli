import json
from pathlib import Path
import subprocess
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import browser_mcp as mcp


CLI = Path(mcp.__file__).with_name("browser-cli")
URL = "https://example.com/"
TAB = {"url": URL, "title": "Example", "id": "42", "window": 1, "tab": 2}
PAGE = {"status": "ok", "title": "Example", "final_url": URL, "text": "Example Domain", "truncated": False}


class ReaderTest(unittest.TestCase):
    def setUp(self):
        self.reader = mcp.Reader(str(CLI))
        mock = patch.object(self.reader, "run")
        self.run_cli = mock.start()
        self.addCleanup(mock.stop)

    def test_requires_choice_without_browser_access(self):
        self.assertEqual(self.reader.read({"url": URL})["status"], "browser_required")
        self.run_cli.assert_not_called()

    def test_rejects_nonweb_and_credential_urls(self):
        for value in ("file:///tmp/test", "https://name:secret@example.com/", "https://example.com/\nother"):
            with self.assertRaises(ValueError):
                self.reader.read({"url": value, "browser": "safari"})
        self.run_cli.assert_not_called()

    def test_safari_matches_exact_url_not_prefix(self):
        self.run_cli.side_effect = [json.dumps([dict(TAB, url=URL + "private"), TAB]), json.dumps(PAGE)]
        self.assertEqual(self.reader.read({"url": URL[:-1], "browser": "safari", "include_links": True})["text"], PAGE["text"])
        self.run_cli.assert_called_with("safari", "read-page", "--tab", URL, "--include-links", "--", URL)

    def test_ambiguous_stops_before_content(self):
        self.run_cli.return_value = json.dumps([TAB, dict(TAB, id="43")])
        self.assertEqual(self.reader.read({"url": URL, "browser": "chrome"})["status"], "tab_ambiguous")
        self.run_cli.assert_called_once_with("chrome", "tabs", "--json")

    def test_chrome_session_requires_confirmed_id(self):
        args = {"url": URL, "browser": "chrome", "mode": "session_tab"}
        self.run_cli.return_value = json.dumps([TAB])
        pending = self.reader.read(args)
        self.assertEqual(pending["status"], "tab_confirmation_required")
        self.assertEqual(pending["candidate"]["id"], "42")
        self.run_cli.assert_called_once_with("chrome", "tabs", "--json")
        self.assertEqual(self.reader.read(dict(args, confirmed_tab_id="43"))["status"], "target_changed")
        self.run_cli.side_effect = [json.dumps([TAB]), json.dumps(PAGE)]
        self.assertEqual(self.reader.read(dict(args, confirmed_tab_id="42"))["status"], "ok")
        self.run_cli.assert_called_with("chrome", "read-page", "--tab", "id:42", "--", URL)

    def test_default_never_opens_missing_tab(self):
        self.run_cli.return_value = "[]"
        self.assertEqual(self.reader.read({"url": URL, "browser": "safari"})["status"], "open_tab_required")
        self.run_cli.assert_called_once_with("safari", "tabs", "--json")

    def test_public_background_never_falls_back_or_installs(self):
        self.run_cli.side_effect = ["[]", mcp.CliFailure(20)]
        response = self.reader.read({"url": URL, "browser": "chrome", "mode": "public_background"})
        self.assertEqual(response["status"], "extension_required")
        self.assertEqual(self.run_cli.call_count, 2)
        self.run_cli.assert_called_with("chrome", "open", "--background", "--", URL)

    def test_background_reads_only_the_returned_tab(self):
        self.run_cli.side_effect = ["[]", "id:41\tcreated", json.dumps([TAB])]
        response = self.reader.read({"url": URL, "browser": "chrome", "mode": "public_background"})
        self.assertEqual(response["status"], "target_changed")
        self.assertEqual(self.run_cli.call_count, 3)

    def test_human_gate_and_login_stop_without_content(self):
        for status in ("verification_required", "login_required", "no_content", "tab_ambiguous"):
            self.run_cli.side_effect = [json.dumps([TAB]), json.dumps({"status": status, "text": "not passed through"})]
            response = self.reader.read({"url": URL, "browser": "safari"})
            self.assertEqual(response["status"], status)
            self.assertNotIn("text", response)


class StdioTest(unittest.TestCase):
    def test_initialize_catalog_and_call(self):
        def req(method, params, id=None):
            return {"jsonrpc": "2.0", "method": method, "params": params, **({"id": id} if id else {})}

        requests = [req("initialize", {"protocolVersion": "2025-11-25"}, 1),
                    req("notifications/initialized", {}), req("tools/list", {}, 2),
                    req("tools/call", {"name": "read_page", "arguments": {"url": URL}}, 3),
                    req("tools/call", {"name": "eval", "arguments": {}}, 4)]
        completed = subprocess.run([CLI, "mcp"], input="\n".join(map(json.dumps, requests)) + "\n",
                                   capture_output=True, text=True, check=True, timeout=10)
        init, listing, call, refused = map(json.loads, completed.stdout.splitlines())
        self.assertEqual(completed.stderr, "")
        self.assertEqual(init["result"]["protocolVersion"], "2025-11-25")
        self.assertEqual([tool["name"] for tool in listing["result"]["tools"]], ["read_page"])
        self.assertEqual(call["result"]["structuredContent"]["status"], "browser_required")
        self.assertEqual(refused["error"]["code"], -32602)
