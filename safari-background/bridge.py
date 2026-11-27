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
MAX_TIMEOUT = 300
OPERATIONS = {"status", "find", "open", "read", "close"}
DEFAULT_BINARY = Path.home() / "Applications/AgentBrowserSafari.app/Contents/MacOS/AgentBrowserSafari"


def request(operation, payload, timeout):
    """Return Chrome-compatible result objects; never retry a possibly delivered write."""
    if operation == "status" and payload is None:
        payload = {}
    if (not isinstance(operation, str) or operation not in OPERATIONS or not isinstance(payload, dict)
            or type(timeout) not in (int, float) or not 0 < timeout <= MAX_TIMEOUT):
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
    try:
        data = json.dumps({"v": 1, "id": message_id, "profile": profile, "operation": operation,
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
        if not isinstance(reply, dict) or type(reply.get("v")) is not int or reply["v"] != 1 or (
            reply.get("id") != message_id or not isinstance(reply.get("status"), str)
        ):
            raise ValueError
        return {key: value for key, value in reply.items() if key not in ("v", "id")}
    except (FileNotFoundError, PermissionError):
        return {"status": "extension_required"}
    except (OSError, subprocess.SubprocessError, ValueError, RecursionError) as error:
        status = ("open_unconfirmed" if operation == "open" else "cleanup_unconfirmed" if operation == "close"
                  else "timeout" if isinstance(error, subprocess.TimeoutExpired) else "disconnected")
        return {"status": status}
