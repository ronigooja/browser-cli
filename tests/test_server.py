import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from browser_backends import ChromeBackend, default_backends
from browser_policy import url_key, url_mismatch
from browser_service import BrowserService
from server import MESSAGE_LIMIT, Server, serve


URL = "https://example.com/page?q=1&q=1#fragment"
ADDED = "https://example.com/page?q=1&q=1&extra=private#fragment"


def tab(ref="browser-instance:tab", url=URL, title="Example"):
    return {"ref": ref, "url": url, "title": title}


class Fake:
    def __init__(self):
        self.calls = []
        self.state = "ok"
        self.update_required = False
        self.wake_state = "disconnected"
        self.tabs = [tab()]
        self.pages = []
        self.opened = None
        self.closed = "closed"
        self.profile_map = {"Default": "Work"}
        self.install_calls = []

    def request(self, operation, payload, timeout):
        self.calls.append((operation, dict(payload), timeout))
        if operation == "status":
            return {"status": self.state, **({"update_required": True} if self.update_required else {})}
        if operation == "wake":
            return {"status": self.wake_state}
        if operation == "find":
            return {"status": "ok", "tabs": self.tabs}
        if operation == "open":
            self.tabs = [self.opened["tab"]]
            return self.opened
        if operation == "read":
            return self.pages.pop(0) if self.pages else {
                "status": "ok", "text": "Visible page text", "final_url": payload["expected_url"]}
        if operation == "close":
            return {"status": self.closed}
        if operation == "uninstall":
            return {"status": getattr(self, "uninstall_status", "uninstall_unverified")}
        raise AssertionError(operation)

    def profiles(self):
        return self.profile_map.copy()

    def install(self, profile):
        self.install_calls.append(profile)
        self.state = "pending"

    def loading_instructions(self):
        return "Use Load unpacked. Select the entire folder."

    def created(self, url=URL, ownership="opaque-proof"):
        self.tabs = []
        self.opened = {"status": "ok", "created": True, "tab": tab(url=url), "ownership": ownership}


class Clock:
    now = 100.0

    def __call__(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds


class ServerTest(unittest.TestCase):
    def setUp(self):
        self.chrome, self.safari, self.clock = Fake(), Fake(), Clock()
        self.service = BrowserService({"chrome": self.chrome, "safari": self.safari}, self.clock, self.clock.sleep)
        self.server = self.start()

    def start(self, protocol="2025-06-18"):
        server = Server(self.service)
        response = server.dispatch({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": protocol}})
        self.assertEqual(response["result"]["protocolVersion"], protocol)
        self.assertIsNone(server.dispatch({"jsonrpc": "2.0", "method": "notifications/initialized"}))
        return server

    def call(self, name, args, owner="test-session", server=None):
        server = server if server is not None else self.server
        params = {"name": name, "arguments": args}
        if owner is not None:
            params["_meta"] = {"ai.opencode/sessionID": owner}
        response = server.dispatch({"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": params})
        self.assertNotIn("error", response, response)
        value = response["result"]
        answer = json.loads(value["content"][0]["text"])
        if "structuredContent" in value:
            self.assertEqual(value["structuredContent"], answer)
        return answer

    def read(self, browser="chrome", url=URL, owner="test-session", server=None, **args):
        if browser is not None:
            args["browser"] = browser
        return self.call("read_page", {"url": url, **args}, owner, server)

    def setup(self, action, browser="chrome", owner="test-session", **args):
        return self.call("setup_extension", {"action": action, "browser": browser, **args}, owner)

    def test_public_reuse_and_browser_required(self):
        self.assertEqual(self.read(browser=None)["status"], "browser_required")
        self.assertEqual(self.chrome.calls, [])
        output = self.read()
        self.assertEqual(output["text"], "Visible page text")
        self.assertNotIn("cleanup_token", output)
        self.assertEqual([call[0] for call in self.chrome.calls], ["status", "find", "read"])
        self.assertEqual(self.safari.calls, [])

    def test_public_chrome_recovers_without_sending_url_to_wake(self):
        self.chrome.state = "disconnected"
        self.chrome.wake_state = "ok"
        self.assertEqual(self.read()["status"], "ok")
        self.assertEqual([(op, data) for op, data, _ in self.chrome.calls], [
            ("status", {}), ("wake", {}), ("find", {"url": URL}),
            ("read", {"ref": tab()["ref"], "expected_url": URL}),
        ])

    def test_recovery_is_not_a_setup_check_or_existing_tab_operation(self):
        self.chrome.state = self.safari.state = "disconnected"
        for call in (lambda: self.setup("check"), lambda: self.read(mode="existing_tab"),
                     lambda: self.read(mode="session_tab"), lambda: self.read(browser="safari")):
            self.assertEqual(call()["status"], "extension_disconnected")
        self.assertEqual([op for op, _, _ in self.chrome.calls], ["status"] * 3)
        self.assertEqual([op for op, _, _ in self.safari.calls], ["status"])

    def test_failed_chrome_wake_does_not_send_a_url(self):
        self.chrome.state = "disconnected"
        self.assertEqual(self.read()["status"], "extension_disconnected")
        self.assertEqual([(op, data) for op, data, _ in self.chrome.calls], [("status", {}), ("wake", {})])
        self.chrome.calls.clear()
        self.chrome.update_required = True
        self.assertTrue(self.read()["update_required"])
        self.assertEqual([(op, data) for op, data, _ in self.chrome.calls], [("status", {})])

    def test_login_confirmation_in_both_browsers(self):
        for browser, backend in (("safari", self.safari), ("chrome", self.chrome)):
            with self.subTest(browser=browser):
                backend.tabs.append(tab("second-ref", title="Identical URL in another tab"))
                output = self.read(browser, mode="session_tab")
                self.assertEqual(output["status"], "tab_ambiguous")
                candidate = output["candidates"][1]
                token = candidate["candidate_token"]
                self.assertNotIn("ref", candidate)
                self.assertNotIn("q=1", candidate["url"])
                self.assertNotIn("read", [call[0] for call in backend.calls])
                self.assertEqual(self.read(browser, owner="other", mode="session_tab", candidate_token=token)["mismatch_reason"], "candidate_unavailable")
                self.assertEqual(self.read(browser, server=self.start(), mode="session_tab", candidate_token=token)["mismatch_reason"], "candidate_unavailable")
                self.assertEqual(self.read(browser, mode="session_tab", candidate_token=token)["status"], "ok")
                self.assertEqual(backend.calls[-1][1], {"ref": "second-ref", "expected_url": URL})

    def test_confirmation_is_pinned_and_expires(self):
        choice = self.read(mode="session_tab")
        self.assertEqual(choice["status"], "tab_confirmation_required")
        token = choice["candidate"]["candidate_token"]
        self.chrome.tabs = [tab("replacement-ref")]
        self.assertEqual(self.read(mode="session_tab", candidate_token=token)["mismatch_reason"], "tab_not_found")
        self.clock.now += 301
        self.assertEqual(self.read(mode="session_tab", candidate_token=token)["mismatch_reason"], "candidate_unavailable")
        self.assertNotIn("read", [call[0] for call in self.chrome.calls])

    def test_existing_and_session_never_open(self):
        self.chrome.tabs = []
        for mode in ("existing_tab", "session_tab"):
            self.assertEqual(self.read(mode=mode)["status"], "open_tab_required")
        self.assertNotIn("open", [call[0] for call in self.chrome.calls])

    def test_resume_and_cleanup_scope_and_original_tab(self):
        self.chrome.created(ADDED)
        opened = self.read()
        self.assertEqual(opened["mismatch_reason"], "query_added")
        self.assertNotIn("private", opened["observed_url"])
        self.assertNotIn("read", [call[0] for call in self.chrome.calls])
        resume, cleanup = opened["resume_token"], opened["cleanup_token"]
        self.assertNotEqual(resume, cleanup)
        self.chrome.tabs = [tab("other-tab"), tab(url=ADDED)]
        retry = {"url_policy": "allow_added_query", "resume_token": resume}
        self.assertEqual(self.read(owner="different", **retry)["mismatch_reason"], "resume_unavailable")
        self.assertEqual(self.read(**retry)["final_url"], ADDED)
        self.assertEqual(self.chrome.calls[-1][1]["ref"], tab()["ref"])
        self.assertEqual(self.call("cleanup_tab", {"cleanup_token": resume})["status"], "cleanup_unavailable")
        self.assertEqual(self.call("cleanup_tab", {"cleanup_token": cleanup}, "different")["status"], "cleanup_unavailable")
        self.assertEqual(self.call("cleanup_tab", {"cleanup_token": cleanup})["status"], "closed")
        self.assertEqual(self.chrome.calls[-1][1], {"ref": tab()["ref"], "ownership": "opaque-proof"})
        self.assertEqual(self.read(**retry)["mismatch_reason"], "resume_unavailable")
        self.assertEqual(self.call("cleanup_tab", {"cleanup_token": cleanup})["status"], "cleanup_unavailable")

    def test_created_human_verification_reuses_original_ref(self):
        self.chrome.created()
        self.chrome.pages = [{"status": "verification_required", "text": "do not return"}]
        output = self.read()
        self.assertEqual(output["status"], "verification_required")
        self.assertNotIn("text", output)
        self.assertNotIn("close", [call[0] for call in self.chrome.calls])
        self.chrome.tabs.append(tab("other-ref"))
        self.assertEqual(self.read(resume_token=output["resume_token"])["status"], "ok")

    def test_no_ownership_passes_no_token(self):
        self.chrome.created(ownership=None)
        output = self.read()
        self.assertEqual(output["status"], "ok")
        self.assertNotIn("cleanup_token", output)
        self.assertIn("cleanup_note", output)

    def test_uncertain_cleanup_is_not_replayed(self):
        self.chrome.created()
        output = self.read()
        self.chrome.closed = "timeout"
        token = {"cleanup_token": output["cleanup_token"]}
        self.assertEqual(self.call("cleanup_tab", token)["status"], "cleanup_unconfirmed")
        self.assertEqual(self.call("cleanup_tab", token)["status"], "cleanup_unavailable")
        self.assertEqual([call[0] for call in self.chrome.calls].count("close"), 1)

    def test_guard_rejects_changed_url_before_challenge_or_text(self):
        self.chrome.pages = [{"status": "ok", "text": "private", "title": "private title", "final_url": ADDED}]
        output = self.read()
        self.assertEqual(output["mismatch_reason"], "query_added")
        self.assertNotIn("text", output)
        self.assertNotIn("title", output)
        self.chrome.pages = [{"status": "login_required", "final_url": "https://example.com/login"}]
        self.assertEqual(self.read()["mismatch_reason"], "path_changed")

    def test_readiness_and_challenge_stop_without_content(self):
        self.chrome.pages = [{"status": "page_loading"}, {"status": "content_not_ready"},
                             {"status": "verification_required", "text": "private"},
                             {"status": "ok", "final_url": URL, "text": "do not request"}]
        output = self.read(wait_for_selector="main a", selector="main", include_links=True)
        self.assertEqual(output["status"], "verification_required")
        self.assertNotIn("text", output)
        self.assertEqual(len(self.chrome.pages), 1)
        self.assertEqual(self.chrome.calls[-1][1]["wait_for_selector"], "main a")
        self.assertEqual(self.chrome.calls[-1][1]["selector"], "main")
        self.chrome.pages = [{"status": "content_not_ready"}] * 50
        start = self.clock.now
        self.assertEqual(self.read(wait_for_selector="main a")["status"], "content_not_ready")
        self.assertEqual(self.clock.now - start, 8)

    def test_passive_challenge_waits_on_same_guard_then_rechecks(self):
        self.chrome.pages = [{"status": "passive_challenge"}] * 12 + [{"status": "verification_required"}]
        start = self.clock.now
        self.assertEqual(self.read()["status"], "verification_required")
        self.assertEqual(self.clock.now - start, 12)
        self.assertEqual({call[1]["expected_url"] for call in self.chrome.calls if call[0] == "read"}, {URL})

    def test_output_is_bounded_and_filters_links(self):
        self.chrome.pages = [{"status": "ok", "text": "😀" * 20000, "title": "a" * 501, "final_url": URL,
                              "private": "ignored", "links": [{"text": "bad", "url": "javascript:hello()"}] +
                              [{"text": "b" * 300, "url": URL}] * 50}]
        output = self.read(include_links=True)
        self.assertTrue(output["truncated"])
        self.assertEqual(len(output["text"]), 6000)
        self.assertEqual(len(output["title"]), 500)
        self.assertEqual(len(output["links"]), 25)
        self.assertNotIn("private", output)

    def test_uninstall_requires_consent_and_does_not_claim_disconnection_as_proof(self):
        self.assertEqual(self.setup("uninstall")["status"], "consent_required")
        self.assertEqual(self.chrome.calls, [])
        self.assertEqual(self.setup("uninstall", approved=True)["status"], "uninstall_unconfirmed")
        self.assertEqual([call[0] for call in self.chrome.calls], ["uninstall"])
        self.assertEqual(self.setup("uninstall", browser="safari", approved=True)["status"], "manual_uninstall_required")
        self.assertEqual(self.safari.calls, [])
        self.chrome.uninstall_status = "ambiguous"
        refused = self.setup("uninstall", approved=True)
        self.assertEqual(refused["status"], "extension_ambiguous")
        self.assertIn("No uninstall request was sent", refused["message"])

    def test_uninstall_cleanup_requires_confirmation_and_no_live_connection(self):
        with patch("browser_setup.cleanup_installation", return_value={"moved": [], "errors": []}) as cleanup:
            self.assertEqual(self.setup("cleanup_uninstall", approved=True)["status"], "consent_required")
            self.assertEqual(self.setup("cleanup_uninstall", approved=True, removal_confirmed=True)["status"], "cleanup_refused")
            cleanup.assert_not_called()
            self.chrome.state = "disconnected"
            self.assertEqual(self.setup("cleanup_uninstall", approved=True, removal_confirmed=True)["status"], "cleanup_complete")
            cleanup.assert_called_once_with("chrome")

    def test_safari_uninstall_cleanup_requires_removal_and_permitted_connection_state(self):
        def request(**args):
            return self.setup("cleanup_uninstall", browser="safari", **args)

        with patch("browser_setup.cleanup_installation", return_value={"moved": [], "errors": []}) as cleanup:
            self.assertEqual(request(approved=True)["status"], "consent_required")
            self.assertEqual(self.safari.calls, [])
            for status in ("ok", "site_access_required", "private_access_enabled_or_unverified", "profile_required", "timeout"):
                self.safari.state = status
                self.assertEqual(request(approved=True, removal_confirmed=True)["status"], "cleanup_refused")
            cleanup.assert_not_called()
            self.safari.state = "disconnected"
            self.assertEqual(request(approved=True, removal_confirmed=True)["status"], "cleanup_complete")
            cleanup.assert_called_once_with("safari")

    def test_setup_consent_profile_connection(self):
        self.chrome.state = "extension_required"
        self.chrome.profile_map["Profile 2"] = "Personal"
        self.assertEqual(self.setup("prepare")["status"], "consent_required")
        self.assertEqual(self.chrome.calls, [])
        prepared = self.setup("prepare", approved=True)
        self.assertEqual(prepared["status"], "profile_required")
        grant = prepared["grant"]
        self.assertEqual(self.setup("install", owner="other", grant=grant)["status"], "grant_required")
        self.assertEqual(self.setup("install", grant=grant)["status"], "profile_required")
        self.assertEqual(self.setup("install", grant=grant, profile="Default")["status"], "extension_pending")
        self.assertIn("Select the entire folder", self.setup("check")["instructions"])
        self.assertEqual(self.setup("install", grant=grant, profile="Default")["status"], "grant_required")
        self.assertEqual(self.chrome.install_calls, ["Default"])
        self.chrome.state = "ok"
        self.assertEqual(self.setup("check")["status"], "extension_connected")

    def test_setup_rechecks_profiles_after_consent(self):
        self.chrome.state = "extension_required"
        grant = self.setup("prepare", approved=True)["grant"]
        self.chrome.profile_map = {"Profile 2": "Personal"}
        self.assertEqual(self.setup("install", grant=grant)["status"], "profiles_changed")
        self.assertEqual(self.setup("install", grant=grant)["status"], "grant_required")
        self.assertEqual(self.chrome.install_calls, [])

    def test_chrome_bridge_statuses_normalize_without_opening(self):
        states = {"extension_required": "extension_required", "missing": "extension_required",
                  "pending": "extension_pending", "disconnected": "extension_disconnected",
                  "ambiguous": "extension_ambiguous"}
        for native, expected in states.items():
            with self.subTest(native=native):
                self.chrome.state = native
                self.chrome.calls.clear()
                self.assertEqual(self.read()["status"], expected)
                self.assertEqual(self.setup("check")["status"], expected)
                read_ops = ["status", "wake"] if native == "disconnected" else ["status"]
                self.assertEqual([call[0] for call in self.chrome.calls], read_ops + ["status"])

    def test_old_protocol_update_requires_explicit_approval_and_single_use_grant(self):
        self.chrome.state, self.chrome.update_required = "disconnected", True
        self.assertTrue(self.read()["update_required"])
        self.assertEqual(self.setup("check")["next_action"], "prepare")
        self.assertEqual(self.setup("prepare", update=True)["status"], "consent_required")
        self.assertNotIn("grant", self.setup("prepare", approved=True))
        prepared = self.setup("prepare", update=True, approved=True)
        self.assertEqual(prepared["status"], "install_ready")
        grant = prepared["grant"]
        self.assertEqual(self.setup("install", owner="other", grant=grant)["status"], "grant_required")
        staged = self.setup("install", grant=grant)
        self.assertEqual(staged["status"], "extension_pending")
        self.assertIn("Reload the existing", staged["instructions"])
        self.assertEqual(self.setup("install", grant=grant)["status"], "grant_required")
        self.chrome.state = "disconnected"  # Old native process may still answer until reload.
        self.assertEqual(self.setup("check")["status"], "extension_pending")
        self.assertNotIn("grant", self.setup("prepare", update=True, approved=True))
        self.chrome.state, self.chrome.update_required = "ok", False
        self.assertEqual(self.setup("check")["status"], "extension_connected")
        self.assertEqual(self.chrome.install_calls, ["Default"])

    def test_ordinary_disconnection_never_reinstalls_and_grant_intent_cannot_change(self):
        self.chrome.state = "disconnected"
        self.assertEqual(self.setup("check")["next_action"], "check")
        self.assertNotIn("grant", self.setup("prepare", update=True, approved=True))
        self.assertNotIn("grant", self.setup("prepare", approved=True))
        self.chrome.state, self.chrome.update_required = "disconnected", True
        update = self.setup("prepare", update=True, approved=True)["grant"]
        self.chrome.state, self.chrome.update_required = "extension_required", False
        self.assertEqual(self.setup("install", grant=update)["status"], "extension_required")
        install = self.setup("prepare", approved=True)["grant"]
        self.chrome.state, self.chrome.update_required = "disconnected", True
        self.assertEqual(self.setup("install", grant=install)["status"], "extension_disconnected")
        self.assertEqual(self.chrome.install_calls, [])

    def test_default_safari_fails_closed_and_production_setup_is_manual(self):
        with tempfile.TemporaryDirectory() as empty:
            server = Server(BrowserService(default_backends(Path(empty))))
            server.protocol, server.ready = "2025-06-18", True
            output = self.read("safari", server=server)
        self.assertEqual(output["reason"], "safari_production_host_unavailable")
        self.safari.state = "permission_required"
        output = self.setup("check", browser="safari")
        self.assertIn("grant all HTTP(S) access once", output["instructions"])
        self.assertIn("AGENT_BROWSER_SAFARI_PROFILE", output["instructions"])
        self.assertEqual(self.safari.install_calls, [])

    def test_chrome_setup_adapter_stages_without_launching_and_does_not_pollute_stdout(self):
        class Bridge:
            def install(self, profile, *, open_settings):
                self.args = profile, open_settings
                print("installer guidance")

        from contextlib import redirect_stderr, redirect_stdout
        bridge, out, err = Bridge(), io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            ChromeBackend(bridge).install("Default")
        self.assertEqual(bridge.args, ("Default", False))
        self.assertEqual(out.getvalue(), "")
        self.assertIn("installer guidance", err.getvalue())

    def test_mcp_stdio_framing_and_tool_list(self):
        server = Server(self.service)
        list_request = {"jsonrpc": "2.0", "method": "tools/list", "id": 2}
        requests = [b"a" * (MESSAGE_LIMIT + 10), b"{", json.dumps(list_request).encode(),
                    b'{"jsonrpc":"2.0","id":3,"method":"initialize","params":{"protocolVersion":"2024-11-05"}}',
                    b'{"jsonrpc":"2.0","method":"notifications/initialized"}', json.dumps(list_request).encode()]
        out = io.StringIO()
        serve(io.BytesIO(b"\n".join(requests)), out, server)
        replies = [json.loads(line) for line in out.getvalue().splitlines()]
        self.assertEqual([reply["error"]["code"] for reply in replies[:3]], [-32700, -32700, -32000])
        self.assertEqual({tool["name"] for tool in replies[-1]["result"]["tools"]},
                         {"read_page", "cleanup_tab", "setup_extension", "browser_tabs", "browser_page", "capture_tab"})

    def test_query_policy_preserves_key_value_multiplicity_and_fragment(self):
        expected = url_key(URL)
        self.assertIsNone(url_mismatch(ADDED, expected, "allow_added_query"))
        self.assertEqual(url_mismatch(ADDED, expected, "exact"), "query_added")
        self.assertEqual(url_mismatch(ADDED.replace("extra=private", "q=2"), expected, "allow_added_query"), "requested_query_changed")
        self.assertEqual(url_mismatch(ADDED.replace("q=1&q=1", "q=1"), expected, "allow_added_query"), "requested_query_changed")
        self.assertEqual(url_mismatch(ADDED.replace("#fragment", "#new"), expected, "allow_added_query"), "fragment_changed")


if __name__ == "__main__":
    unittest.main()
