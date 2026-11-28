import unittest

from browser_operations import BrowserOperations
from browser_service import BrowserService


URL = "https://example.com/list"
NEXT = "https://example.com/detail"
TAB = {"ref": "instance:42", "title": "List", "url": URL, "active": False, "window_id": 1}


class Backend:
    def __init__(self):
        self.calls = []
        self.replies = {"list": {"status": "ok", "tabs": [TAB]}, "find": {"status": "ok", "tabs": [TAB]},
                        "status": {"status": "ok"}, "page": {"status": "ok", "final_url": URL}}

    def request(self, operation, payload, timeout):
        self.calls.append((operation, payload))
        return self.replies[operation]


class BrowserOperationTest(unittest.TestCase):
    def setUp(self):
        self.backend = Backend()
        self.other = Backend()
        self.scope = object()
        self.service = BrowserService({"chrome": self.backend, "safari": self.other})
        self.ops = BrowserOperations(self.service)

    def tabs(self, action, **fields):
        return self.ops.tabs(dict(action=action, browser="chrome", **fields), self.scope)

    def page(self, action, ref, **fields):
        return self.ops.page(dict(action=action, browser="chrome", tab_ref=ref, **fields), self.scope)

    def candidate(self):
        row = self.tabs("list")["tabs"][0]
        self.assertNotIn("tab_ref", row)
        self.assertNotIn("ref", row)
        return row["candidate_token"]

    def target(self):
        return self.tabs("confirm", candidate_token=self.candidate(), approved=True)["tab_ref"]

    def test_scope_and_confirm(self):
        candidate = self.candidate()
        self.assertEqual(self.tabs("confirm", candidate_token=candidate, approved=False)["status"], "approval_required")
        self.assertEqual(self.page("click", candidate, selector="button")["status"], "target_changed")
        self.assertEqual(self.ops.tabs(dict(browser="chrome", action="confirm", candidate_token=candidate, approved=True),
                                       object())["status"], "target_changed")
        target = self.tabs("confirm", candidate_token=candidate, approved=True)["tab_ref"]
        self.assertEqual(self.tabs("confirm", candidate_token=candidate, approved=True)["status"], "target_changed")
        self.assertEqual(self.page("snapshot", target)["status"], "ok")
        self.assertEqual(self.backend.calls[-1], ("page", {"kind": "snapshot", "ref": TAB["ref"], "expected_url": URL}))
        self.assertEqual(self.other.calls, [])

    def test_confirm_checks_exact_target_again(self):
        candidate = self.candidate()
        self.backend.replies["find"]["tabs"] = [dict(TAB, url=URL + "?q=1")]
        self.assertEqual(self.tabs("confirm", candidate_token=candidate, approved=True)["status"], "target_changed")

    def test_open_owns_only_created_background_tabs(self):
        self.backend.replies["open"] = {"status": "ok", "tab": TAB, "created": False, "ownership": "fake"}
        reused = self.tabs("open_background", url=URL)
        self.assertIn("candidate_token", reused)
        self.assertNotIn("cleanup_token", reused)
        self.assertNotIn("tab_ref", reused)
        self.backend.replies["open"]["created"] = True
        created = self.tabs("open_background", url=URL)
        self.assertIn("tab_ref", created)
        self.assertIn("cleanup_token", created)
        self.backend.replies["close"] = {"status": "closed"}
        self.assertEqual(self.service.cleanup({"cleanup_token": created["cleanup_token"]}, self.scope)["status"], "closed")

    def test_approved_mutations_and_ambiguous_result_not_retried(self):
        ref = self.target()
        before = len(self.backend.calls)
        self.assertEqual(self.page("submit", ref, selector="form", approved=False)["status"], "approval_required")
        self.assertEqual(self.tabs("activate", tab_ref=ref, approved=False)["status"], "approval_required")
        self.assertEqual(len(self.backend.calls), before)
        self.backend.replies["page"] = {"status": "timeout"}
        self.assertEqual(self.page("click", ref, element_ref="element")["status"], "action_unconfirmed")
        self.assertEqual(len(self.backend.calls), before + 1)
        self.assertEqual(self.backend.calls[-1][1]["elementRef"], "element")

    def test_navigation_rotates_reference_and_checks_destination(self):
        ref = self.target()
        self.backend.replies["tab"] = {"status": "ok", "tab": dict(TAB, url=NEXT)}
        new = self.tabs("navigate", url=NEXT, tab_ref=ref)["tab_ref"]
        self.assertNotEqual(ref, new)
        self.assertEqual(self.page("info", ref)["status"], "target_changed")
        self.backend.replies["page"] = {"status": "ok", "final_url": NEXT}
        self.assertEqual(self.page("info", new)["status"], "ok")

    def test_invalid_fields_never_reach_browser(self):
        ref = self.target()
        before = len(self.backend.calls)
        for action, fields in (("scroll", {"direction": "by"}), ("click", {"selector": "a", "element_ref": "x"}),
                               ("wait", {"condition": "invalid"}), ("eval", {"text": "1 + 1"})):
            with self.assertRaises(ValueError):
                self.page(action, ref, **fields)
        self.assertEqual(len(self.backend.calls), before)

    def test_cross_origin_navigation_requires_reconfirmation(self):
        ref = self.target()
        destination = "https://other.example/detail"
        self.backend.replies["tab"] = {"status": "ok", "tab": dict(TAB, url=destination)}
        result = self.tabs("navigate", url=destination, tab_ref=ref)
        self.assertEqual(result["status"], "ok")
        self.assertNotIn("tab_ref", result)
        self.assertEqual(self.page("info", ref)["status"], "target_changed")

    def test_update_requirement_is_preserved_for_page_operations(self):
        ref = self.target()
        self.backend.replies["page"] = {"status": "disconnected", "update_required": True}
        self.assertTrue(self.page("snapshot", ref)["update_required"])
