"""In-process transport adapters for the packaged browser extensions."""

import importlib.util
from contextlib import redirect_stdout
from pathlib import Path
import sys


class Unavailable:
    def __init__(self, reason):
        self.reason = reason

    def request(self, operation, payload, timeout):
        return {"status": "backend_unavailable", "reason": self.reason}


class ExtensionBackend:
    """Transport refs must be bound to a browser instance/profile and not recycled.

    A read must verify the ref and exact expected_url in the main document before
    challenge/login detection, readiness or extraction. A close must independently
    verify the ref and ownership marker, never just a tab number or URL.
    """

    def __init__(self, bridge):
        self.bridge = bridge

    def request(self, operation, payload, timeout):
        return self.bridge.request(operation, payload, timeout)


class ChromeBackend(ExtensionBackend):
    def profiles(self):
        return self.bridge.discover_profiles() or {"Default": "Default"}

    def install(self, profile):
        # The native host installer may print CLI guidance. Stdio is reserved for MCP.
        with redirect_stdout(sys.stderr):
            self.bridge.install(profile, open_settings=False)

    def loading_instructions(self):
        return self.bridge.loading_instructions()


def load_extension(path, name):
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise ImportError(name)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    try:
        exec(compile(path.read_bytes(), str(path), "exec"), module.__dict__)
        if not callable(getattr(module, "request", None)):
            raise ImportError(name)
    except Exception:
        sys.modules.pop(name, None)
        raise
    return module


def default_backends(root=None):
    root = Path(root) if root is not None else Path(__file__).resolve().parent
    routes = {
        "chrome": ("chrome-background", ChromeBackend, "chrome_transport_unavailable"),
        "safari": ("safari-background", ExtensionBackend, "safari_production_host_unavailable"),
    }
    backends = {}
    for name, (folder, adapter, reason) in routes.items():
        try:
            bridge = load_extension(root / folder / "bridge.py", f"_agent_browser_{name}_bridge")
        except (ImportError, OSError, SyntaxError):
            backends[name] = Unavailable(reason)
        else:
            backends[name] = adapter(bridge)
    return backends
