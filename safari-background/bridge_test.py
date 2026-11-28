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

    def test_new_operations_use_the_v2_envelope(self):
        def host(argv, **kwargs):
            self.assertEqual(argv[1:], ["ipc"])
            message = json.loads(kwargs["input"])
            self.assertEqual(message["v"], 2)
            self.assertEqual(message["operation"], "list")
            self.assertEqual(message["payload"], {})
            return subprocess.CompletedProcess(argv, 0, json.dumps({"v": 2, "id": message["id"],
                                                                    "status": "ok", "tabs": []}).encode())

        with patch.object(bridge.subprocess, "run", side_effect=host):
            self.assertEqual(bridge.request("list", None, 5), {"status": "ok", "tabs": []})

    def test_matching_v1_rejection_reports_update_without_retry(self):
        def old_host(argv, **kwargs):
            message = json.loads(kwargs["input"])
            return subprocess.CompletedProcess(argv, 0, json.dumps({"v": 1, "id": message["id"],
                                                                    "status": "invalid_request"}).encode())

        with patch.object(bridge.subprocess, "run", side_effect=old_host) as run:
            self.assertEqual(bridge.request("tab", {"kind": "close"}, 5),
                             {"status": "disconnected", "update_required": True})
            run.assert_called_once()

    def test_subprocess_timeout_distinguishes_reads_from_possible_writes(self):
        with patch.object(bridge.subprocess, "run", side_effect=subprocess.TimeoutExpired("ipc", 5)):
            for operation, payload, status in (("page", {"kind": "info"}, "timeout"),
                                                ("page", {"kind": "click"}, "action_unconfirmed"),
                                                ("tab", {"kind": "navigate"}, "action_unconfirmed"),
                                                ("front_open", {}, "action_unconfirmed"),
                                                ("open", {}, "open_unconfirmed"),
                                                ("close", {}, "cleanup_unconfirmed"),
                                                ("capture", {}, "timeout")):
                with self.subTest(operation=operation, payload=payload):
                    self.assertEqual(bridge.request(operation, payload, 5), {"status": status})

    def test_uninstall_plan_requires_user_action_without_dispatch(self):
        with patch.object(bridge.subprocess, "run") as run:
            plan = bridge.uninstall_plan()
        run.assert_not_called()
        self.assertEqual(plan["status"], "manual_removal_required")
        self.assertIn("Safari > Settings > Extensions", plan["instructions"])
