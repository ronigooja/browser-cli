"""Internal Safari request adapter; never launches Safari or installs anything."""

import json
import math
import os
from pathlib import Path
import secrets
import subprocess
import time
from uuid import UUID


LIMIT = 65536
VERSION = 3
MAX_TIMEOUT = 300
OPERATIONS = {"status", "find", "open", "read", "close", "list", "page", "tab", "front_open", "capture", "capture_chunk",
              "qualify_window"}
PAGE_MUTATIONS = ("click", "scroll", "fill", "select", "press", "submit")
QUALIFICATION_BLOCK_REASONS = frozenset((
    "arm_missing_or_invalid", "safari_not_running", "foreground_unverified", "screen_capture_unavailable",
    "window_snapshot_unavailable", "accessibility_untrusted", "safari_windows_present", "lease_expired",
    "permissions_unverified", "mailbox_unavailable",
))
DEFAULT_BINARY = Path.home() / "Applications/AgentBrowserSafari.app/Contents/MacOS/AgentBrowserSafari"


def uninstall_plan():
    """Manual guidance only. Transport or filesystem evidence cannot prove Safari removal."""
    return {
        "status": "manual_removal_required",
        "instructions": (
            "Check Agent Browser in Safari > Settings > Extensions in each named profile. "
            "If you choose to remove it, disable background requests in its setup popup, "
            "turn off the extension in each enabled profile, and follow Safari's instructions "
            "to uninstall the signed AgentBrowserSafari app. Clear AGENT_BROWSER_SAFARI_PROFILE "
            "and any AGENT_BROWSER_SAFARI_BINARY override from your MCP configuration. "
            "A disconnected response does not verify removal. No files or Safari settings were changed."
        ),
    }


def request(operation, payload, timeout):
    """Return Chrome-compatible result objects; never retry a possibly delivered write."""
    if operation in ("status", "list") and payload is None:
        payload = {}
    if (not isinstance(operation, str) or operation not in OPERATIONS or not isinstance(payload, dict)
            or type(timeout) not in (int, float) or not 0 < timeout <= MAX_TIMEOUT):
        return {"status": "invalid_request"}
    qualification = operation == "qualify_window"
    if qualification and payload:
        return {"status": "invalid_request"}
    profile = os.environ.get("AGENT_BROWSER_SAFARI_PROFILE", "")
    try:
        profile = str(UUID(profile))
    except ValueError:
        return {"status": "profile_required"}
    binary = Path(os.environ.get("AGENT_BROWSER_SAFARI_BINARY", str(DEFAULT_BINARY))).expanduser()
    if not binary.is_absolute():
        return {"status": "extension_required"}
    message_id = secrets.token_hex(16)
    expires = math.ceil((time.time() + timeout) * 1000)
    uncertain = ("unconfirmed" if qualification else "open_unconfirmed" if operation == "open" else "cleanup_unconfirmed" if operation == "close"
                 else "action_unconfirmed" if operation in ("tab", "front_open") or (
                     operation == "page" and payload.get("kind") in PAGE_MUTATIONS) else None)
    try:
        data = json.dumps({"v": VERSION, "id": message_id, "profile": profile, "operation": operation,
                           "payload": payload, "expires": expires}, separators=(",", ":"), allow_nan=False).encode()
        if len(data) > LIMIT:
            return {"status": "invalid_request"}
    except (TypeError, ValueError, RecursionError):
        return {"status": "invalid_request"}
    try:
        out = subprocess.run([str(binary), "ipc"], input=data, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                             timeout=timeout + 0.5, check=True).stdout
        if not 0 < len(out) <= LIMIT + 1:
            raise ValueError
        reply = json.loads(out)
        if not isinstance(reply, dict) or type(reply.get("v")) is not int or (
            reply.get("id") != message_id or not isinstance(reply.get("status"), str)
        ):
            raise ValueError
        if (reply["v"], reply["status"]) in ((1, "invalid_request"), (2, "protocol_mismatch")):
            # Known old hosts reject a v3 request before they can publish a lease.
            return {"status": "disconnected", "update_required": True}
        if reply["v"] != VERSION:
            raise ValueError
        if qualification:
            candidate = set(reply) == {"v", "id", "status"} and reply["status"] == "candidate_observed"
            if (set(reply) == {"v", "id", "status", "reason"} and reply["status"] == "qualification_blocked"
                    and isinstance(reply["reason"], str) and reply["reason"] in QUALIFICATION_BLOCK_REASONS):
                return {"status": "qualification_blocked", "reason": reply["reason"]}
            return {"status": "candidate_observed" if candidate else "unconfirmed"}
        return {key: value for key, value in reply.items() if key not in ("v", "id")}
    except (FileNotFoundError, PermissionError):
        return {"status": "extension_required"}
    except (OSError, subprocess.SubprocessError, ValueError, RecursionError) as error:
        status = uncertain or ("timeout" if isinstance(error, subprocess.TimeoutExpired) else "disconnected")
        return {"status": status}
