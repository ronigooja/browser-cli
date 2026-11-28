import json
import os
import subprocess
import unittest
from unittest.mock import patch

import bridge


class BridgeTest(unittest.TestCase):
    def setUp(self):
        env = patch.dict(os.environ, {"AGENT_BROWSER_SAFARI_PROFILE": "266a0f53-e756-42d5-8a0a-37931f95a830"})
        env.start()
        self.addCleanup(env.stop)

    def test_new_operations_use_the_v3_envelope(self):
        payload = {"image_ref": "a" * 32, "offset": 0}

        def host(argv, **kwargs):
            self.assertEqual(argv[1:], ["ipc"])
            message = json.loads(kwargs["input"])
            self.assertEqual(message["v"], 3)
            self.assertEqual(message["operation"], "capture_chunk")
            self.assertEqual(message["payload"], payload)
            return subprocess.CompletedProcess(argv, 0, json.dumps({"v": 3, "id": message["id"],
                                                                     "status": "ok", "data": "AAAA", "next_offset": 4}).encode())

        with patch.object(bridge.subprocess, "run", side_effect=host):
            self.assertEqual(bridge.request("capture_chunk", payload, 5), {"status": "ok", "data": "AAAA", "next_offset": 4})

    def test_matching_v1_or_v2_rejection_reports_update_without_retry(self):
        for version, status in ((1, "invalid_request"), (2, "protocol_mismatch")):
            def old_host(argv, **kwargs):
                message = json.loads(kwargs["input"])
                return subprocess.CompletedProcess(argv, 0, json.dumps({"v": version, "id": message["id"],
                                                                         "status": status}).encode())

            with self.subTest(version=version), patch.object(bridge.subprocess, "run", side_effect=old_host) as run:
                self.assertEqual(bridge.request("status", {}, 5),
                                 {"status": "disconnected", "update_required": True})
                run.assert_called_once()

    def test_qualification_accepts_only_empty_payload_and_returns_no_browser_data(self):
        with patch.object(bridge.subprocess, "run") as run:
            for payload in (None, {"url": "https://example.com/"}, {"arm": "true"}):
                self.assertEqual(bridge.request("qualify_window", payload, 5), {"status": "invalid_request"})
            run.assert_not_called()

        blocked = "qualification_blocked"
        cases = [("candidate_observed", {}, {"status": "candidate_observed"}),
                 ("candidate_observed", {"ref": "unsafe"}, {"status": "unconfirmed"}),
                 ("unconfirmed", {}, {"status": "unconfirmed"}), ("ok", {}, {"status": "unconfirmed"}),
                 (blocked, {}, {"status": "unconfirmed"}),
                 (blocked, {"reason": "other"}, {"status": "unconfirmed"}),
                 (blocked, {"reason": ["lease_expired"]}, {"status": "unconfirmed"}),
                 (blocked, {"reason": "lease_expired", "ref": "unsafe"}, {"status": "unconfirmed"}),
                 (blocked, {"reason": "lease_expired", "id": "0" * 32}, {"status": "unconfirmed"})]
        cases.extend((blocked, {"reason": reason}, {"status": blocked, "reason": reason})
                     for reason in bridge.QUALIFICATION_BLOCK_REASONS)
        for status, extras, expected in cases:
            def host(argv, **kwargs):
                message = json.loads(kwargs["input"])
                self.assertEqual((message["operation"], message["payload"]), ("qualify_window", {}))
                return subprocess.CompletedProcess(argv, 0, json.dumps({"v": 3, "id": message["id"],
                                                                         "status": status, **extras}).encode())

            with self.subTest(status=status, extras=extras), patch.object(bridge.subprocess, "run", side_effect=host) as run:
                self.assertEqual(bridge.request("qualify_window", {}, 5), expected)
                run.assert_called_once()

    def test_subprocess_timeout_distinguishes_reads_from_possible_writes(self):
        with patch.object(bridge.subprocess, "run", side_effect=subprocess.TimeoutExpired("ipc", 5)):
            for operation, payload, status in (("page", {"kind": "info"}, "timeout"),
                                                ("page", {"kind": "click"}, "action_unconfirmed"),
                                                ("tab", {"kind": "navigate"}, "action_unconfirmed"),
                                                ("front_open", {}, "action_unconfirmed"),
                                                ("open", {}, "open_unconfirmed"),
                                                ("close", {}, "cleanup_unconfirmed"),
                                                ("qualify_window", {}, "unconfirmed"),
                                                ("capture", {}, "timeout")):
                with self.subTest(operation=operation, payload=payload):
                    self.assertEqual(bridge.request(operation, payload, 5), {"status": status})

    def test_uninstall_plan_requires_user_action_without_dispatch(self):
        with patch.object(bridge.subprocess, "run") as run:
            plan = bridge.uninstall_plan()
        run.assert_not_called()
        self.assertEqual(plan["status"], "manual_removal_required")
        self.assertIn("Safari > Settings > Extensions", plan["instructions"])
