import json
from pathlib import Path
import subprocess
import sys
import unittest
from unittest.mock import call, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import agent_browser_mcp as mcp


CLI = Path(mcp.__file__).with_name("agent-browser")
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

    def test_safari_uses_unique_observed_url_with_exact_document_guard(self):
        selected = URL + "?added=1"
        self.run_cli.side_effect = [json.dumps([dict(TAB, url=selected), dict(TAB, url="https://elsewhere.test/?u=" + URL)]),
                                    json.dumps(dict(PAGE, final_url=selected))]
        page = self.reader.read({"url": URL, "browser": "safari", "url_policy": "allow_added_query",
                                 "include_links": True, "selector": "main"})
        self.assertEqual(page["text"], PAGE["text"])
        self.run_cli.assert_called_with("safari", "read-page", "--tab", selected, "--include-links", "--selector", "main", "--", selected)

    def test_safari_rejects_prefix_path_in_existing_mode(self):
        self.run_cli.return_value = json.dumps([dict(TAB, url=URL + "posts")])
        response = self.reader.read({"url": URL, "browser": "safari", "mode": "existing_tab"})
        self.assertEqual(response["status"], "open_tab_required")
        self.run_cli.assert_called_once_with("safari", "tabs", "--json")

    def test_safari_stops_on_ambiguous_substring(self):
        self.run_cli.return_value = json.dumps([TAB, dict(TAB, url=URL + "posts")])
        self.assertEqual(self.reader.read({"url": URL, "browser": "safari"})["status"], "tab_ambiguous")
        self.run_cli.assert_called_once_with("safari", "tabs", "--json")

    def test_safari_does_not_read_matching_url_at_a_different_returned_index(self):
        self.run_cli.side_effect = ["[]", "1:2\treused", json.dumps([dict(TAB, tab=3), dict(TAB, url=URL + "?extra=1")])]
        response = self.reader.read({"url": URL, "browser": "safari"})
        self.assertEqual(response["mismatch_reason"], "tab_identity_unverified")
        self.assertNotIn("observed_url", response)
        self.assertEqual(self.run_cli.call_count, 3)

    def test_ambiguous_stops_before_content(self):
        self.run_cli.return_value = json.dumps([TAB, dict(TAB, id="43", tab=3)])
        page = self.reader.read({"url": URL, "browser": "chrome"})
        self.assertEqual(page["status"], "tab_ambiguous")
        self.assertEqual(page["candidate_count"], 2)
        self.assertEqual([tab["tab"] for tab in page["candidates"]], [2, 3])
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
        with patch.object(mcp.time, "monotonic", side_effect=[0, 4]):
            response = self.reader.read({"url": URL, "browser": "chrome", "mode": "public_background"})
        self.assertEqual(response["status"], "target_changed")
        self.assertEqual(response["mismatch_reason"], "tab_not_found")
        self.assertNotIn("cleanup_token", response)
        self.assertNotIn("observed_url", response)
        self.assertEqual(self.run_cli.call_count, 3)

    def test_prefix_only_reused_tab_is_not_reported_as_target_change(self):
        tab = dict(TAB, url=URL + "post/123")
        self.run_cli.side_effect = [json.dumps([tab]), "id:42\treused", json.dumps([tab])]
        with patch.object(mcp.time, "monotonic", side_effect=[0, 4]):
            response = self.reader.read({"url": URL, "browser": "chrome"})
        self.assertEqual(response["status"], "reused_tab_mismatch")
        self.assertEqual(self.run_cli.call_count, 3)
        self.assertNotIn("cleanup_token", response)

    def test_reused_chrome_tab_can_finish_an_exact_pending_navigation(self):
        self.run_cli.side_effect = ["[]", "id:42\treused", "[]", json.dumps([TAB]), json.dumps(PAGE)]
        with patch.object(mcp.time, "sleep") as delay:
            response = self.reader.read({"url": URL, "browser": "chrome"})
        delay.assert_called_once_with(0.25)
        self.assertEqual(response["status"], "ok")
        self.assertEqual(response["tab_disposition"], "reused")
        self.assertNotIn("cleanup_token", response)

    def test_reused_chrome_tab_with_wrong_id_is_not_read(self):
        self.run_cli.side_effect = ["[]", "id:41\treused", json.dumps([TAB])]
        with patch.object(mcp.time, "monotonic", side_effect=[0, 4]):
            response = self.reader.read({"url": URL, "browser": "chrome"})
        self.assertEqual(response["status"], "reused_tab_mismatch")
        self.assertEqual(self.run_cli.call_count, 3)
        self.assertNotIn("cleanup_token", response)
        self.assertNotIn("observed_url", response)

    def test_exact_normalizes_host_default_port_and_empty_path(self):
        self.run_cli.side_effect = [json.dumps([TAB]), json.dumps(PAGE)]
        response = self.reader.read({"url": "https://EXAMPLE.com:443", "browser": "chrome", "mode": "existing_tab"})
        self.assertEqual(response["status"], "ok")
        self.run_cli.assert_called_with("chrome", "read-page", "--tab", "id:42", "--", URL)

    def test_added_query_policy_accepts_only_returned_chrome_tab_for_both_dispositions(self):
        requested = URL + "search?q=hello%20world&tag=x&tag=x&blank=#results"
        observed = URL + "search?tag=x&blank&scope=public&tag=x&q=hello+world#results"
        for disposition in ("created", "reused"):
            with self.subTest(disposition=disposition):
                other = dict(TAB, id="43", url=requested)
                self.run_cli.side_effect = ["[]", f"id:42\t{disposition}", json.dumps([other, dict(TAB, url=observed)]),
                                            json.dumps(dict(PAGE, final_url=observed))]
                response = self.reader.read({"url": requested, "browser": "chrome", "url_policy": "allow_added_query"})
                self.assertEqual(response["status"], "ok")
                self.assertEqual(response["tab_disposition"], disposition)
                self.assertEqual("cleanup_token" in response, disposition == "created")
                self.run_cli.assert_called_with("chrome", "read-page", "--tab", "id:42", "--", observed)

    def test_exact_mismatch_reports_safe_retry_and_preserves_proven_ownership(self):
        requested = URL + "search?q=keyword#section"
        observed = URL + "search?q=keyword&added=private#section"
        self.run_cli.side_effect = ["[]", "id:42\tcreated", json.dumps([dict(TAB, url=observed)])]
        with patch.object(mcp.time, "monotonic", side_effect=[0, 4, 4]):
            response = self.reader.read({"url": requested, "browser": "chrome"}, scope="owner")
        self.assertEqual(response["status"], "target_changed")
        self.assertEqual(response["mismatch_reason"], "query_added")
        self.assertEqual(response["observed_url"], URL + "search?REDACTED#REDACTED")
        self.assertEqual(response["retry_url_policy"], "allow_added_query")
        self.assertTrue(response["cleanup_token"])
        self.assertEqual(self.run_cli.call_count, 3)
        self.run_cli.side_effect = [json.dumps([dict(TAB, url=observed), dict(TAB, id="43", url=observed)]),
                                    json.dumps(dict(PAGE, final_url=observed))]
        args = {"url": requested, "browser": "chrome", "url_policy": response["retry_url_policy"],
                "resume_token": response["resume_token"]}
        self.assertEqual(self.reader.read(args, scope="other")["mismatch_reason"], "resume_unavailable")
        self.assertEqual(self.reader.read(dict(args, url=URL), scope="owner")["mismatch_reason"], "resume_unavailable")
        with patch.object(mcp.time, "monotonic", return_value=4):
            retry = self.reader.read(args, scope="owner")
        self.assertEqual(retry["status"], "ok")
        self.assertNotIn("cleanup_token", retry)
        self.run_cli.assert_called_with("chrome", "read-page", "--tab", "id:42", "--", observed)

    def test_new_tab_waits_for_document_commit_without_relaxing_guard(self):
        for browser, opening, spec in (("safari", "1:2\tcreated", URL), ("chrome", "id:42\tcreated", "id:42")):
            self.run_cli.reset_mock()
            tabs = json.dumps([TAB])
            self.run_cli.side_effect = ["[]", opening, tabs,
                                        json.dumps({"status": "target_changed", "document_url": "about:blank"}),
                                        tabs, *([tabs] if browser == "safari" else []), json.dumps(PAGE)]
            with self.subTest(browser=browser), patch.object(mcp.time, "sleep"):
                self.assertEqual(self.reader.read({"url": URL, "browser": browser})["status"], "ok")
            self.run_cli.assert_called_with(browser, "read-page", "--tab", spec, "--", URL, timeout=5)

    def test_wrong_document_url_stops_even_if_browser_metadata_matches(self):
        self.run_cli.side_effect = ["[]", "id:42\tcreated", json.dumps([TAB]), json.dumps({
            "status": "target_changed", "document_url": "https://different.test/path?q=private"})]
        page = self.reader.read({"url": URL, "browser": "chrome"})
        self.assertEqual(page["mismatch_reason"], "origin_changed")
        self.assertEqual(page["observed_url"], "https://different.test/path?REDACTED")
        self.assertEqual(self.run_cli.call_count, 4)

    def test_added_query_policy_stops_on_mutation_pollution_path_origin_and_fragment(self):
        requested = URL + "search?q=keyword&q=keyword#section"
        cases = {
            "requested_query_changed": (
                URL + "search?q=different&q=keyword#section",
                URL + "search?q=keyword#section",
                URL + "search?q=keyword&q=keyword&q=keyword#section",
                URL + "search?q=keyword&q=keyword&%71=different#section",
            ),
            "path_changed": (URL + "other?q=keyword&q=keyword#section",),
            "origin_changed": ("https://elsewhere.test/search?q=keyword&q=keyword#section",),
            "fragment_changed": (URL + "search?q=keyword&q=keyword#other",),
            "query_changed": (URL + "search?q=keyword&q=keyword&extra=%FF#section",),
        }
        for reason, urls in cases.items():
            for observed in urls:
                with self.subTest(observed=observed):
                    self.run_cli.reset_mock()
                    self.run_cli.side_effect = ["[]", "id:42\treused", json.dumps([dict(TAB, url=observed),
                                                                                  dict(TAB, id="43", url=requested)])]
                    with patch.object(mcp.time, "monotonic", side_effect=[0, 4]):
                        response = self.reader.read({"url": requested, "browser": "chrome", "url_policy": "allow_added_query"})
                    self.assertEqual(response["status"], "reused_tab_mismatch")
                    self.assertEqual(response["mismatch_reason"], reason)
                    self.assertIn("observed_url", response)
                    self.assertNotIn("retry_url_policy", response)
                    self.assertNotIn("cleanup_token", response)
                    self.assertEqual(self.run_cli.call_count, 3)

    def test_guard_mismatch_reports_only_current_metadata_of_same_chrome_id(self):
        observed = URL + "?extra=private"
        self.run_cli.side_effect = [json.dumps([TAB]), json.dumps({"status": "target_changed", "text": "discard"}),
                                    json.dumps([dict(TAB, url=observed), dict(TAB, id="43")])]
        response = self.reader.read({"url": URL, "browser": "chrome"})
        self.assertEqual(response["status"], "target_changed")
        self.assertEqual(response["observed_url"], URL + "?REDACTED")
        self.assertEqual(response["mismatch_reason"], "query_added")
        self.assertNotIn("text", response)
        self.run_cli.assert_called_with("chrome", "tabs", "--json")

    def test_inconsistent_success_does_not_return_content_or_url_secrets(self):
        observed = "https://elsewhere.test/?secret=token#private"
        self.run_cli.side_effect = [json.dumps([TAB]), json.dumps(dict(PAGE, final_url=observed))]
        response = self.reader.read({"url": URL, "browser": "chrome"})
        self.assertEqual(response["mismatch_reason"], "origin_changed")
        self.assertEqual(response["observed_url"], "https://elsewhere.test/?REDACTED#REDACTED")
        self.assertNotIn("text", response)

    def test_chrome_guard_renews_exact_url_only_on_same_id_and_allowed_query_change(self):
        observed = URL + "?extra=1"
        self.run_cli.side_effect = [json.dumps([TAB]), json.dumps({"status": "target_changed"}),
                                    json.dumps([dict(TAB, url=observed), dict(TAB, id="43")]),
                                    json.dumps(dict(PAGE, final_url=observed))]
        response = self.reader.read({"url": URL, "browser": "chrome", "url_policy": "allow_added_query"})
        self.assertEqual(response["status"], "ok")
        self.run_cli.assert_called_with("chrome", "read-page", "--tab", "id:42", "--", observed)

    def test_created_id_already_present_does_not_read_or_claim_user_tab(self):
        self.run_cli.side_effect = [json.dumps([dict(TAB, url=URL + "different")]), "id:42\tcreated"]
        response = self.reader.read({"url": URL, "browser": "chrome"})
        self.assertEqual(response["mismatch_reason"], "created_tab_id_already_present")
        self.assertNotIn("cleanup_token", response)
        self.assertEqual(self.run_cli.call_count, 2)

    def test_existing_mode_suggests_only_bounded_query_retry(self):
        self.run_cli.return_value = json.dumps([dict(TAB, url=URL + "?extra=private")])
        response = self.reader.read({"url": URL, "browser": "chrome", "mode": "existing_tab"})
        self.assertEqual(response["status"], "target_changed")
        self.assertEqual(response["retry_url_policy"], "allow_added_query")
        self.run_cli.assert_called_once_with("chrome", "tabs", "--json")

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
        failure = mcp.CliFailure(26, "agent-browser: AppleScript error -1728: missing tab")
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
        self.run_cli.side_effect = [json.dumps([TAB]), json.dumps({"status": "passive_challenge"}),
                                    *([json.dumps([TAB]), json.dumps({"status": "passive_challenge"})] * 4),
                                    json.dumps([TAB]), json.dumps({"status": "verification_required"})]
        with patch.object(mcp.time, "monotonic", side_effect=lambda: now[0]), \
                patch.object(mcp.time, "sleep", side_effect=lambda delay: now.__setitem__(0, now[0] + delay)):
            response = self.reader.read({"url": URL, "browser": "safari"})
        self.assertEqual(now[0], 5)
        self.assertEqual(response["status"], "verification_required")
        self.assertNotIn("text", response)

    def test_passive_wait_respects_deadline_and_caps_last_probe(self):
        now = [0]
        self.run_cli.side_effect = [json.dumps([TAB]), json.dumps({"status": "passive_challenge"}),
                                    *([json.dumps([TAB]), json.dumps({"status": "passive_challenge"})] * 2)]
        with patch.object(mcp, "PASSIVE_TIMEOUT", 2.5), \
                patch.object(mcp.time, "monotonic", side_effect=lambda: now[0]), \
                patch.object(mcp.time, "sleep", side_effect=lambda delay: now.__setitem__(0, now[0] + delay)):
            response = self.reader.read({"url": URL, "browser": "safari"})
        self.assertEqual(now[0], 2.5)
        self.assertEqual(response["status"], "passive_challenge")
        self.assertEqual(self.run_cli.call_count, 6)
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

    def test_cleanup_distinguishes_instance_missing_tab_and_unconfirmed(self):
        for raw, expected in (("instance_changed", "cleanup_instance_changed"),
                              ("not_found", "cleanup_tab_not_found"), ("unexpected", "cleanup_unconfirmed")):
            self.run_cli.return_value = raw
            token = self.reader.claim("owner", "id:42", "instance")
            args = {"cleanup_token": token}
            with self.subTest(expected=expected):
                self.assertEqual(self.reader.cleanup(args, "owner")["status"], expected)
                self.assertEqual(self.reader.cleanup(args, "owner")["status"], "cleanup_unavailable")

    def test_new_tab_waits_for_content_on_same_tab(self):
        now = [0]
        self.run_cli.side_effect = ["[]", "id:42\tcreated", json.dumps([TAB]),
                                    json.dumps({"status": "no_content"}), json.dumps(PAGE)]
        with patch.object(mcp.time, "monotonic", side_effect=lambda: now[0]), \
                patch.object(mcp.time, "sleep", side_effect=lambda delay: now.__setitem__(0, now[0] + delay)):
            response = self.reader.read({"url": URL, "browser": "chrome"})
        self.assertEqual((now[0], response["status"]), (0.5, "ok"))
        self.assertEqual(self.run_cli.call_args_list[-2:], [
            call("chrome", "read-page", "--tab", "id:42", "--", URL),
            call("chrome", "read-page", "--tab", "id:42", "--", URL, timeout=5),
        ])

    def test_new_tab_empty_wait_is_bounded_and_stops_for_blockers(self):
        for status in ("no_content", "login_required", "verification_required", "target_changed"):
            now = [0]
            empty = json.dumps({"status": "no_content"})
            probes = [empty, *([json.dumps([TAB]), empty] * 15)] if status == "no_content" else [empty, json.dumps([TAB]), json.dumps({"status": status})]
            if status == "target_changed":
                probes.append(json.dumps([TAB]))
            self.run_cli.side_effect = ["[]", "1:2\tcreated", json.dumps([TAB]), *probes]
            with self.subTest(status=status), patch.object(mcp.time, "monotonic", side_effect=lambda: now[0]), \
                    patch.object(mcp.time, "sleep", side_effect=lambda delay: now.__setitem__(0, now[0] + delay)):
                response = self.reader.read({"url": URL, "browser": "safari"})
                self.assertEqual(response["status"], status)
                self.assertEqual(now[0], 8 if status == "no_content" else 0.5)

    def test_empty_reused_tab_does_not_wait(self):
        self.run_cli.side_effect = ["[]", "id:42\treused", json.dumps([TAB]), json.dumps({"status": "no_content"})]
        self.assertEqual(self.reader.read({"url": URL, "browser": "chrome"})["status"], "no_content")
        self.assertEqual(self.run_cli.call_count, 4)

    def test_explicit_readiness_waits_on_existing_tab_without_navigation(self):
        now = [0]
        selector = 'main a[href*="/status/"]'
        self.run_cli.side_effect = [json.dumps([TAB]), json.dumps({"status": "content_not_ready"}), json.dumps(PAGE)]
        with patch.object(mcp.time, "monotonic", side_effect=lambda: now[0]), \
                patch.object(mcp.time, "sleep", side_effect=lambda delay: now.__setitem__(0, now[0] + delay)):
            page = self.reader.read({"url": URL, "browser": "chrome", "mode": "existing_tab",
                                     "selector": "main", "wait_for_selector": selector})
        self.assertEqual((page["status"], now[0]), ("ok", 0.5))
        probe = ("chrome", "read-page", "--tab", "id:42", "--selector", "main", "--wait-for-selector", selector, "--", URL)
        self.assertEqual(self.run_cli.call_args_list, [call("chrome", "tabs", "--json"), call(*probe), call(*probe, timeout=5)])

    def test_readiness_timeout_keeps_created_tab_cleanup_token(self):
        now = [0]
        self.run_cli.side_effect = ["[]", "id:42\tcreated", json.dumps([TAB]),
                                    *([json.dumps({"status": "content_not_ready"})] * 16)]
        with patch.object(mcp.time, "monotonic", side_effect=lambda: now[0]), \
                patch.object(mcp.time, "sleep", side_effect=lambda delay: now.__setitem__(0, now[0] + delay)):
            page = self.reader.read({"url": URL, "browser": "chrome", "wait_for_selector": "article"})
        self.assertEqual((page["status"], now[0]), ("content_not_ready", 8))
        self.assertNotIn("text", page)
        self.assertTrue(page["cleanup_token"])

    def test_readiness_does_not_reopen_closed_target(self):
        self.run_cli.side_effect = [json.dumps([TAB]), json.dumps({"status": "content_not_ready"}), mcp.CliFailure(26), "[]"]
        with patch.object(mcp.time, "sleep"):
            page = self.reader.read({"url": URL, "browser": "chrome", "wait_for_selector": "article"})
        self.assertEqual(page["status"], "open_tab_required")
        self.assertEqual(self.run_cli.call_count, 4)

    def test_readiness_stops_for_login_verification_and_navigation(self):
        for status in ("login_required", "verification_required", "target_changed", "invalid_wait_selector"):
            self.run_cli.reset_mock()
            self.run_cli.side_effect = [json.dumps([TAB]), json.dumps({"status": "content_not_ready"}), json.dumps({"status": status})]
            if status == "target_changed":
                self.run_cli.side_effect = [json.dumps([TAB]), json.dumps({"status": "content_not_ready"}),
                                            json.dumps({"status": status}), json.dumps([TAB])]
            with self.subTest(status=status), patch.object(mcp.time, "sleep"):
                page = self.reader.read({"url": URL, "browser": "chrome", "wait_for_selector": "article"})
                self.assertEqual(page["status"], status)
                self.assertNotIn("text", page)
                self.assertEqual(self.run_cli.call_count, 4 if status == "target_changed" else 3)

    def test_safari_readiness_stops_if_the_url_moves_to_another_index(self):
        self.run_cli.side_effect = [json.dumps([TAB]), json.dumps({"status": "content_not_ready"}),
                                    json.dumps([dict(TAB, tab=3)])]
        with patch.object(mcp.time, "sleep"):
            page = self.reader.read({"url": URL, "browser": "safari", "wait_for_selector": "article"})
        self.assertEqual(page["mismatch_reason"], "tab_identity_unverified")
        self.assertEqual(self.run_cli.call_count, 3)

    def test_reused_and_safari_indices_never_grant_cleanup(self):
        for browser, opening in (("chrome", "id:42\treused"), ("safari", "1:2\tcreated")):
            self.run_cli.side_effect = ["[]", opening, json.dumps([TAB]), json.dumps(PAGE)]
            response = self.reader.read({"url": URL, "browser": browser})
            self.assertEqual(response["status"], "ok")
            self.assertNotIn("cleanup_token", response)

    def test_chrome_restart_during_open_never_reads_or_grants_cleanup(self):
        self.instance.side_effect = ["first", "second"]
        self.run_cli.side_effect = ["[]", "id:42\tcreated", json.dumps([TAB]), json.dumps(PAGE)]
        response = self.reader.read({"url": URL, "browser": "chrome"})
        self.assertEqual(response["mismatch_reason"], "browser_instance_changed")
        self.assertNotIn("cleanup_token", response)
        self.assertEqual(self.run_cli.call_count, 3)

    def test_chrome_without_verified_instance_does_not_grant_cleanup(self):
        self.instance.return_value = None
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
        properties = listing["result"]["tools"][0]["inputSchema"]["properties"]
        self.assertEqual(properties["mode"]["default"], "public_background")
        self.assertEqual(properties["url_policy"]["default"], "exact")
        self.assertEqual(properties["wait_for_selector"]["type"], "string")
        self.assertEqual(call["result"]["structuredContent"]["status"], "browser_required")
        self.assertEqual(refused["error"]["code"], -32602)
