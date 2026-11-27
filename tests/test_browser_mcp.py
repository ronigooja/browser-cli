import json
from pathlib import Path
import subprocess
import sys
import unittest
from unittest.mock import call, patch

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
        preflight = patch.object(self.reader, "check_status", return_value="on")
        self.preflight = preflight.start()
        self.addCleanup(preflight.stop)
        instance = patch.object(self.reader, "chrome_instance", return_value="test-instance")
        self.instance = instance.start()
        self.addCleanup(instance.stop)

    def test_requires_choice_without_browser_access(self):
        public = self.reader.read({"url": URL})
        session = self.reader.read({"url": URL, "mode": "session_tab"})
        self.assertEqual(public["status"], "browser_required")
        self.assertNotIn("login", public["message"])
        self.assertIn("required login", session["message"])
        self.run_cli.assert_not_called()

    def test_rejects_nonweb_and_credential_urls(self):
        for value in ("file:///tmp/test", "https://name:secret@example.com/", "https://example.com/\nother"):
            with self.assertRaises(ValueError):
                self.reader.read({"url": value, "browser": "safari"})
        self.run_cli.assert_not_called()

    def test_safari_uses_unique_substring_with_exact_document_guard(self):
        selected = URL + "posts"
        self.run_cli.side_effect = [json.dumps([dict(TAB, url=selected), dict(TAB, url="https://elsewhere.test/?u=" + URL)]),
                                    json.dumps(dict(PAGE, final_url=selected))]
        page = self.reader.read({"url": URL, "browser": "safari", "include_links": True, "selector": "main"})
        self.assertEqual(page["text"], PAGE["text"])
        self.run_cli.assert_called_with("safari", "read-page", "--tab", URL, "--include-links", "--selector", "main", "--", selected)

    def test_safari_stops_on_ambiguous_substring(self):
        self.run_cli.return_value = json.dumps([TAB, dict(TAB, url=URL + "posts")])
        self.assertEqual(self.reader.read({"url": URL, "browser": "safari"})["status"], "tab_ambiguous")
        self.run_cli.assert_called_once_with("safari", "tabs", "--json")

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

    def test_browser_choice_then_default_background_open(self):
        self.assertEqual(self.reader.read({"url": URL})["status"], "browser_required")
        self.run_cli.assert_not_called()
        self.run_cli.side_effect = ["[]", "1:2\tcreated", json.dumps([TAB]), json.dumps(PAGE)]
        self.assertEqual(self.reader.read({"url": URL, "browser": "safari"})["status"], "ok")
        self.assertEqual(self.run_cli.call_args_list, [
            call("safari", "tabs", "--json"), call("safari", "open", "--background", "--", URL),
            call("safari", "tabs", "--json"), call("safari", "read-page", "--tab", URL, "--", URL),
        ])

    def test_existing_and_session_modes_never_open_missing_tab(self):
        for mode in ("existing_tab", "session_tab"):
            self.run_cli.reset_mock()
            self.run_cli.return_value = "[]"
            self.assertEqual(self.reader.read({"url": URL, "browser": "safari", "mode": mode})["status"], "open_tab_required")
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

    def test_disabled_permission_stops_before_tabs_and_keeps_diagnostic(self):
        self.preflight.side_effect = mcp.CliFailure(25, "Enable the browser setting.")
        response = self.reader.read({"url": URL, "browser": "safari"})
        self.assertEqual(response["status"], "permission_required")
        self.assertEqual(response["diagnostic"], "Enable the browser setting.")
        self.run_cli.assert_not_called()

    def test_unknown_permissions_rechecked_after_open(self):
        self.preflight.side_effect = ["unknown", "on"]
        self.run_cli.side_effect = ["[]", "1:2\tcreated", json.dumps([TAB]), json.dumps(PAGE)]
        self.assertEqual(self.reader.read({"url": URL, "browser": "safari"})["status"], "ok")
        self.assertEqual(self.preflight.call_count, 2)

    def test_1728_with_tabs_or_failed_check_preserves_original_and_stops(self):
        failure = mcp.CliFailure(26, "browser-cli: AppleScript error -1728: missing tab")
        for check in (json.dumps([TAB]), mcp.CliFailure(1)):
            self.run_cli.reset_mock()
            self.run_cli.side_effect = [json.dumps([TAB]), failure, check]
            response = self.reader.read({"url": URL, "browser": "safari"})
            self.assertEqual(response["diagnostic"], failure.diagnostic)
            self.assertEqual(self.run_cli.call_count, 3)

    def test_1728_with_no_tabs_recovers_with_new_tab_and_status_check(self):
        self.run_cli.side_effect = [json.dumps([TAB]), mcp.CliFailure(26), "[]", "id:43\tcreated",
                                    json.dumps([dict(TAB, id="43")]), json.dumps(PAGE)]
        self.assertEqual(self.reader.read({"url": URL, "browser": "chrome"})["status"], "ok")
        self.assertEqual(self.preflight.call_count, 2)
        self.run_cli.assert_called_with("chrome", "read-page", "--tab", "id:43", "--", URL)

    def test_passive_wait_continues_past_four_seconds_and_stops_for_human(self):
        now = [0]
        self.run_cli.side_effect = [json.dumps([TAB]), *([json.dumps({"status": "passive_challenge"})] * 5),
                                    json.dumps({"status": "verification_required"})]
        with patch.object(mcp.time, "monotonic", side_effect=lambda: now[0]), \
                patch.object(mcp.time, "sleep", side_effect=lambda delay: now.__setitem__(0, now[0] + delay)):
            response = self.reader.read({"url": URL, "browser": "safari"})
        self.assertEqual(now[0], 5)
        self.assertEqual(response["status"], "verification_required")
        self.assertNotIn("text", response)

    def test_passive_wait_respects_deadline_and_caps_last_probe(self):
        now = [0]
        self.run_cli.side_effect = [json.dumps([TAB]), *([json.dumps({"status": "passive_challenge"})] * 3)]
        with patch.object(mcp, "PASSIVE_TIMEOUT", 2.5), \
                patch.object(mcp.time, "monotonic", side_effect=lambda: now[0]), \
                patch.object(mcp.time, "sleep", side_effect=lambda delay: now.__setitem__(0, now[0] + delay)):
            response = self.reader.read({"url": URL, "browser": "safari"})
        self.assertEqual(now[0], 2.5)
        self.assertEqual(response["status"], "passive_challenge")
        self.assertEqual(self.run_cli.call_count, 4)
        self.run_cli.assert_called_with("safari", "read-page", "--tab", URL, "--", URL, timeout=1)

    def test_cleanup_is_scoped_exact_and_single_use(self):
        self.run_cli.side_effect = ["[]", "id:42\tcreated", json.dumps([TAB]), json.dumps(PAGE), "closed"]
        response = self.reader.read({"url": URL, "browser": "chrome"}, scope="owner")
        args = {"cleanup_token": response["cleanup_token"]}
        self.assertEqual(self.reader.cleanup(args, "other")["status"], "cleanup_unavailable")
        self.assertEqual(self.reader.cleanup(args, "owner")["status"], "closed")
        self.run_cli.assert_called_with("chrome", "close-created", "--tab", "id:42", "--", "test-instance")
        self.assertEqual(self.reader.cleanup(args, "owner")["status"], "cleanup_unavailable")
        self.assertEqual(self.run_cli.call_count, 5)

    def test_reused_and_safari_indices_never_grant_cleanup(self):
        for browser, opening in (("chrome", "id:42\treused"), ("safari", "1:2\tcreated")):
            self.run_cli.side_effect = ["[]", opening, json.dumps([TAB]), json.dumps(PAGE)]
            response = self.reader.read({"url": URL, "browser": browser})
            self.assertEqual(response["status"], "ok")
            self.assertNotIn("cleanup_token", response)

    def test_chrome_restart_during_open_never_grants_cleanup(self):
        self.instance.side_effect = ["first", "second"]
        self.run_cli.side_effect = ["[]", "id:42\tcreated", json.dumps([TAB]), json.dumps(PAGE)]
        response = self.reader.read({"url": URL, "browser": "chrome"})
        self.assertEqual(response["status"], "ok")
        self.assertNotIn("cleanup_token", response)


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
        self.assertEqual([tool["name"] for tool in listing["result"]["tools"]],
                         ["read_page", mcp.SETUP_TOOL["name"], "cleanup_tab"])
        self.assertEqual(listing["result"]["tools"][0]["inputSchema"]["properties"]["mode"]["default"], "public_background")
        self.assertEqual(call["result"]["structuredContent"]["status"], "browser_required")
        self.assertEqual(refused["error"]["code"], -32602)
