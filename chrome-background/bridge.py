"""Installer, local request client, and native messaging host for Chrome."""

import ast
import base64
from contextlib import ExitStack, contextmanager
import ctypes
import hashlib
import json
import os
from pathlib import Path
import plistlib
import select
import secrets
import shlex
import signal
import socket
import stat
import struct
import sys
import tempfile
import time


HOST = "com.agent_browser.background"
CHROME = Path.home() / "Library/Application Support/Google/Chrome"
INSTALL = Path.home() / "Library/Application Support/agent-browser/chrome-background"
LIMIT = 65536
PROTOCOL = 4
OPERATIONS = {"status", "find", "open", "read", "close", "list", "page", "tab", "front_open", "capture", "capture_chunk", "uninstall"}
WAKE_WAIT = 2
WAKE_POLL = 0.05


def runtime_dir(*, create=True):
    # macOS limits Unix socket paths to 103 bytes, regardless of the home directory.
    home = hashlib.sha256(os.fsencode(Path.home())).hexdigest()[:12]
    directory = Path(f"/tmp/agent-browser-{os.getuid()}-{home}")
    if create:
        directory.mkdir(mode=0o700, exist_ok=True)
    info = directory.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
        raise ValueError("Unsafe agent-browser socket directory")
    return directory


def frame(message):
    data = json.dumps(message, separators=(",", ":"), allow_nan=False).encode("utf-8")
    if len(data) > LIMIT:
        raise ValueError("Chrome background message is too large")
    return struct.pack("=I", len(data)) + data


class InvalidFrameSize(ValueError):
    pass


def receive(fd, deadline):
    def exact(size):
        result = bytearray()
        while len(result) < size:
            remaining = max(0, deadline - time.monotonic())
            if not select.select([fd], [], [], remaining)[0]:
                raise TimeoutError
            chunk = os.read(fd, size - len(result))
            if not chunk:
                raise EOFError
            result.extend(chunk)
        return result

    size, = struct.unpack("=I", exact(4))
    if not 0 < size <= LIMIT:
        raise InvalidFrameSize("Invalid Chrome background message size")
    result = json.loads(exact(size))
    if not isinstance(result, dict):
        raise ValueError("Invalid Chrome background message")
    return result


def native_request(message):
    expires = message.get("expires")
    remaining = (expires - int(time.time() * 1000)) / 1000 if type(expires) is int else 0
    if not 0 < remaining <= 86400:
        raise ValueError("Invalid or expired Chrome background deadline")
    if (type(message.get("v")) is not int or message["v"] != PROTOCOL
            or message.keys() != {"v", "id", "operation", "payload", "expires"}
            or not isinstance(message["id"], str) or len(message["id"]) != 32
            or not isinstance(message["operation"], str) or message["operation"] not in OPERATIONS
            or not isinstance(message["payload"], dict)):
        raise ValueError("Invalid Chrome background request")
    return frame(message), remaining


def response_too_large(request):
    operation = request.get("operation")
    status = ("open_unconfirmed" if operation in ("open", "front_open") else
              "action_unconfirmed" if operation in ("page", "tab") else
              "uninstall_unverified" if operation == "uninstall" else
              "cleanup_unconfirmed" if operation == "close" else "response_too_large")
    return {"v": PROTOCOL, "id": request.get("id"), "status": status}


def serve(listener):
    while True:
        # A readable native stdin with no request in flight indicates disconnection.
        ready, _, _ = select.select([0, listener], [], [])
        if 0 in ready:
            return
        client, _ = listener.accept()
        with client:
            poison = False
            try:
                request = receive(client.fileno(), time.monotonic() + 1)
            except (EOFError, ValueError, TimeoutError, OSError):
                continue

            try:
                request_bytes, remaining = native_request(request)
            except ValueError as error:
                status = "timeout" if type(request.get("expires")) is int and request["expires"] <= time.time() * 1000 else "invalid_request"
                if request.get("operation") == "close":
                    status = "cleanup_unconfirmed"
                reply = {"v": PROTOCOL, "id": request.get("id"), "status": status}
            else:
                # A missing reply terminates this connection; a late reply cannot
                # be mistaken for the next operation. The extension reconnects.
                sys.stdout.buffer.write(request_bytes)
                sys.stdout.buffer.flush()
                try:
                    reply = receive(0, time.monotonic() + remaining)
                except InvalidFrameSize:
                    # Do not drain an arbitrary amount of native input or reuse a
                    # stream with unread bytes; reply explicitly before disconnecting.
                    reply = response_too_large(request)
                    poison = True

            try:
                try:
                    response_bytes = frame(reply)
                except ValueError:
                    # Native input is UTF-8; re-encoding as ASCII may expand it.
                    # Never lose a write result without marking it unconfirmed.
                    response_bytes = frame(response_too_large(request))
                client.settimeout(1)
                client.sendall(response_bytes)
            except (OSError, ValueError):
                pass
            if poison:
                return


def native_host(origin):
    if origin != extension_origin(Path(__file__).resolve().parent / "extension"):
        raise ValueError("Unexpected native messaging origin")
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
    with tempfile.TemporaryDirectory(prefix="host-", dir=runtime_dir()) as directory:
        endpoint = str(Path(directory) / "socket")
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as listener:
            listener.bind(endpoint)
            listener.listen()
            try:
                serve(listener)
            except (EOFError, TimeoutError, BrokenPipeError, ValueError):
                pass


def remaining_time(deadline):
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise TimeoutError
    return remaining


@contextmanager
def connected_client(deadline):
    try:
        directory = runtime_dir(create=False)
    except FileNotFoundError:
        yield None
        return
    with ExitStack() as stack:
        connected = None
        for endpoint in sorted(directory.glob("host-*/socket")):
            client = stack.enter_context(socket.socket(socket.AF_UNIX, socket.SOCK_STREAM))
            client.settimeout(remaining_time(deadline))
            try:
                client.connect(str(endpoint))
            except (FileNotFoundError, ConnectionRefusedError):
                client.close()
                continue
            if connected is not None:
                raise AmbiguousConnection("The agent-browser extension is connected in more than one Chrome profile. Leave it enabled only in the profile you want to use.")
            connected = client
        yield connected


class AmbiguousConnection(ValueError):
    pass


def request(operation, payload, timeout):
    """Call the installed extension without Apple Events or launching Chrome.

    Only the local wake operation may request a silent profile load before status.
    The timeout is in seconds. Transport failures never replay uncertain writes.
    """
    wake = isinstance(operation, str) and operation == "wake"
    if wake:
        if not isinstance(payload, dict) or payload:
            return {"status": "invalid_request"}
        operation = "status"
    if operation == "status" and payload is None:
        payload = {}
    if (not isinstance(operation, str) or operation not in OPERATIONS or not isinstance(payload, dict)
            or type(timeout) not in (int, float) or not 0 < timeout <= 86400):
        return {"status": "invalid_request"}
    deadline = time.monotonic() + timeout
    message_id = secrets.token_hex(16)
    try:
        data = frame({"v": PROTOCOL, "id": message_id, "operation": operation, "payload": payload,
                      "expires": int((time.time() + timeout) * 1000)})
    except (TypeError, ValueError, RecursionError):
        return {"status": "invalid_request"}
    dispatched = False
    try:
        with ExitStack() as stack:
            client = stack.enter_context(connected_client(deadline))
            if client is None:
                state = setup_state(ignore_empty=True)
                if operation == "close":
                    return {"status": "cleanup_instance_changed"}
                if state != "missing" and staged_update_required():
                    return {"status": "disconnected", "update_required": True}
                if wake and state == "loaded":
                    client = stack.enter_context(woken_client(deadline))
                if client is None:
                    return {"status": {"missing": "extension_required", "pending": "pending", "loaded": "disconnected"}[state]}
            # Older hosts and workers reject this version before dispatching any write.
            client.settimeout(remaining_time(deadline))
            dispatched = True
            client.sendall(data)
            reply = receive(client.fileno(), deadline)
            if type(reply.get("v")) is not int or reply["v"] != PROTOCOL or reply.get("id") != message_id:
                # A v1 host rejects the request itself; a v1 worker may instead
                # reject it behind a newer host. Neither needs a replay or probe.
                old_host_or_worker = (type(reply.get("v")) is int and 1 <= reply["v"] < PROTOCOL
                                      and reply.get("id") == message_id
                                      and reply.get("status") in ("invalid_request", "protocol_mismatch"))
                if old_host_or_worker or ("v" not in reply and "error" in reply):
                    if operation == "close":
                        return {"status": "cleanup_instance_changed"}
                    return {"status": "disconnected", "update_required": True}
                raise ValueError("Invalid Chrome background reply")
            if not isinstance(reply.get("status"), str):
                raise ValueError("Invalid Chrome background status")
            if reply["status"] == "protocol_mismatch":
                return {"status": "cleanup_instance_changed"} if operation == "close" else {
                    "status": "disconnected", "update_required": True}
            if operation == "uninstall" and reply["status"] in ("uninstall_dispatched", "uninstall_unverified"):
                return {"status": "uninstall_unverified"}
            return {key: value for key, value in reply.items() if key not in ("v", "id")}
    except AmbiguousConnection:
        return {"status": "cleanup_unconfirmed" if operation == "close" else "ambiguous"}
    except (OSError, EOFError, ValueError, RecursionError) as error:
        if operation == "close":
            return {"status": "cleanup_unconfirmed"}
        if operation in ("open", "front_open") and dispatched:
            return {"status": "open_unconfirmed"}
        if operation in ("page", "tab") and dispatched:
            return {"status": "action_unconfirmed"}
        if operation == "uninstall" and dispatched:
            return {"status": "uninstall_unverified"}
        if operation != "status" and isinstance(error, TimeoutError):
            return {"status": "timeout"}
        return {"status": "disconnected"}


def extension_origin(directory):
    manifest = json.loads((directory / "manifest.json").read_text(encoding="utf-8"))
    digest = hashlib.sha256(base64.b64decode(manifest["key"], validate=True)).hexdigest()[:32]
    identifier = digest.translate(str.maketrans("0123456789abcdef", "abcdefghijklmnop"))
    return f"chrome-extension://{identifier}/"


def host_manifest():
    return CHROME / "NativeMessagingHosts" / f"{HOST}.json"


def loaded_profiles(origin, *, strict=False, ignore_empty=False):
    # Chrome records unpacked extensions in either profile preferences file.
    # A missing entry is not proof that an extension is absent (e.g. custom profiles).
    identifier = origin.split("/")[2]
    for filename in ("Preferences", "Secure Preferences"):
        for path in CHROME.glob(f"*/{filename}"):
            try:
                preferences = json.loads(path.read_text(encoding="utf-8"))
                settings = preferences.get("extensions", {}).get("settings", {})
                if identifier in settings and (not ignore_empty or settings[identifier]):
                    yield path.parent.name
            except (OSError, ValueError, AttributeError, TypeError):
                if strict:
                    raise ValueError(f"Could not check Chrome profile {path.parent.name!r}; no files removed") from None
                continue


def setup_state(*, ignore_empty=True):
    origin = extension_origin(Path(__file__).resolve().parent / "extension")
    # Chrome can leave an empty entry after removal.
    if any(loaded_profiles(origin, ignore_empty=ignore_empty)):
        return "loaded"
    if host_manifest().is_file() and (INSTALL / "extension/manifest.json").is_file():
        return "pending"
    return "missing"


def staged_update_required():
    source = Path(__file__).resolve().parent / "extension" / "manifest.json"
    target = INSTALL / "extension" / "manifest.json"
    if not target.is_file():
        return False
    try:
        current = json.loads(source.read_bytes())["version"]
        installed = json.loads(target.read_bytes())["version"]
        return current != installed or not (target.parent / "read-page.js").is_file()
    except (OSError, ValueError, KeyError, TypeError):
        return True


def loading_instructions():
    return (
        "At chrome://extensions/, turn on Developer mode and click Load unpacked.\n"
        "In the folder picker, press Command+Shift+G, paste this path, and press Return:\n"
        f"  {INSTALL / 'extension'}\n"
        "If manifest.json and background.js are greyed out, you are in the right folder. "
        "Do not select either file. Click Select to load the entire folder.\n"
        "If the extension is already loaded, click Reload instead."
    )


class ConnectionRequired(ValueError):
    def __init__(self, state, message):
        super().__init__(message)
        self.state = state


def check_setup():
    result = request("status", {}, 1)
    if result["status"] == "ok":
        print("Chrome background setup complete.")
        return True
    if result.get("update_required"):
        raise ConnectionRequired("loaded", "Agent Browser must be updated before connecting. With the user's approval, "
                                 "stage the current version using the setup service, then reload it in the intended regular Chrome profile.")
    if result["status"] == "ambiguous":
        raise AmbiguousConnection("Leave Agent Browser enabled in only one regular Chrome profile before checking again.")
    return False


def discover_profiles():
    try:
        state = json.loads((CHROME / "Local State").read_text(encoding="utf-8"))
    except FileNotFoundError:
        state = {}
    profile = state.get("profile", {})
    cache = profile.get("info_cache", {})
    names = {key: info.get("name") or key for key, info in cache.items()
             if key not in (".", "..", "System Profile", "Guest Profile")
             and Path(key).name == key and (CHROME / key).is_dir()
             and not info.get("is_omitted")}
    return {key: names[key] for key in sorted(names, key=lambda key: (key != profile.get("last_used"), key))}


def wake_profile():
    """Return a verified regular profile, or None if there is insufficient evidence."""
    source = Path(__file__).resolve().parent
    extension = INSTALL / "extension"
    launcher = INSTALL / "native-host"
    host = INSTALL / "bridge.py"
    manifest = host_manifest()
    local_state = CHROME / "Local State"
    files = (local_state, manifest, host, launcher, extension / "manifest.json",
             extension / "background.js", extension / "read-page.js")
    if (any(path.is_symlink() for path in (CHROME, manifest.parent, INSTALL.parent, INSTALL, extension, *files))
            or not all(path.is_file() for path in files)):
        return None
    origin = extension_origin(source / "extension")
    registration = json.loads(manifest.read_bytes())
    if (registration.get("name") != HOST or registration.get("type") != "stdio"
            or registration.get("path") != str(launcher) or registration.get("allowed_origins") != [origin]
            or not os.access(launcher, os.X_OK)):
        return None
    for name, bundled in (("manifest.json", source / "extension/manifest.json"),
                          ("background.js", source / "extension/background.js"),
                          ("read-page.js", source.parent / "extension/shared/read-page.js")):
        if (extension / name).read_bytes() != bundled.read_bytes():
            return None
    # Existing installs need not contain this local-only recovery code. Verify their
    # native protocol and the installer-generated launcher without executing either.
    nodes = ast.parse(host.read_bytes()).body
    versions = [node.value.value for node in nodes if isinstance(node, ast.Assign)
                and len(node.targets) == 1 and isinstance(node.targets[0], ast.Name)
                and node.targets[0].id == "PROTOCOL" and isinstance(node.value, ast.Constant)]
    if len(versions) != 1 or type(versions[0]) is not int or versions[0] != PROTOCOL:
        return None
    script = launcher.read_text(encoding="utf-8")
    args = shlex.split(script.splitlines()[1])
    if (len(args) != 6 or not Path(args[1]).is_absolute() or not os.access(args[1], os.X_OK)
            or script != native_launcher(args[1], host).decode()):
        return None

    identifier = origin.split("/")[2]
    profiles = {}
    developer_profiles = set()
    disabled_profiles = set()
    for directory in CHROME.iterdir():
        if not directory.is_dir():
            continue
        for name in ("Preferences", "Secure Preferences"):
            path = directory / name
            if path.is_symlink():
                return None
            try:
                if not stat.S_ISREG(path.stat().st_mode):
                    return None
                preferences = json.loads(path.read_bytes()).get("extensions", {})
            except FileNotFoundError:
                continue
            settings = preferences.get("settings", {})
            if not isinstance(settings, dict):
                return None
            entry = settings.get(identifier, {})
            if entry != {}:
                profiles.setdefault(directory.name, []).append(entry)
            developer_mode = preferences.get("ui", {}).get("developer_mode")
            if developer_mode is True:
                developer_profiles.add(directory.name)
            if preferences.get("disabled") or developer_mode is False:
                disabled_profiles.add(directory.name)
    if len(profiles) != 1:
        return None
    profile, = profiles
    attrs = json.loads(local_state.read_bytes())["profile"]["info_cache"][profile]
    if (profile not in developer_profiles or profile in disabled_profiles
            or profile in ("System Profile", "Guest Profile") or profile.startswith("-") or "\\" in profile
            or not profile.isprintable() or profile != profile.strip() or (CHROME / profile).is_symlink()
            or attrs.get("is_omitted") or attrs.get("is_ephemeral") or attrs.get("force_signin_profile_locked")):
        return None
    for entry in profiles[profile]:
        reasons = entry.get("disable_reasons", [])
        if (type(entry.get("location")) is not int or entry["location"] != 4
                or entry.get("path") != str(extension) or not (reasons == [] or type(reasons) is int and reasons == 0)
                or ("state" in entry and (type(entry["state"]) is not int or entry["state"] != 1))
                or any(entry.get(key, 0) for key in ("blacklist", "blacklist_state", "omaha_blocklist_state",
                                                   "extension_telemetry_service_blocklist_state"))):
            return None
    return profile


def native_function(library, name, result, *args):
    function = getattr(library, name)
    function.restype = result
    function.argtypes = args
    return function


def chrome_executable(pid):
    libproc = ctypes.CDLL("/usr/lib/libSystem.B.dylib")
    proc_pidpath = native_function(libproc, "proc_pidpath", ctypes.c_int,
                                   ctypes.c_int, ctypes.c_void_p, ctypes.c_uint32)
    path = ctypes.create_string_buffer(4096)
    if not 0 < proc_pidpath(pid, path, len(path)) < len(path):
        return None
    executable = Path(os.fsdecode(path.value))
    if executable.parts[-3:] != ("Contents", "MacOS", "Google Chrome"):
        return None
    bundle = plistlib.loads((executable.parents[1] / "Info.plist").read_bytes())
    if bundle.get("CFBundleIdentifier") != "com.google.Chrome" or bundle.get("CFBundleExecutable") != executable.name:
        return None
    return executable


def chrome_has_no_windows(pid):
    """Fail closed on unavailable Quartz data; count off-screen Chrome windows too."""
    quartz = ctypes.CDLL("/System/Library/Frameworks/CoreGraphics.framework/CoreGraphics")
    foundation = ctypes.CDLL("/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation")
    ptr, index = ctypes.c_void_p, ctypes.c_long
    session_info = native_function(quartz, "CGSessionCopyCurrentDictionary", ptr)
    window_info = native_function(quartz, "CGWindowListCopyWindowInfo", ptr, ctypes.c_uint32, ctypes.c_uint32)
    release = native_function(foundation, "CFRelease", None, ptr)
    count = native_function(foundation, "CFArrayGetCount", index, ptr)
    get_item = native_function(foundation, "CFArrayGetValueAtIndex", ptr, ptr, index)
    get_value = native_function(foundation, "CFDictionaryGetValue", ptr, ptr, ptr)
    number_value = native_function(foundation, "CFNumberGetValue", ctypes.c_ubyte, ptr, index, ptr)
    type_id = native_function(foundation, "CFGetTypeID", ctypes.c_ulong, ptr)
    number_type = native_function(foundation, "CFNumberGetTypeID", ctypes.c_ulong)()
    make_string = native_function(foundation, "CFStringCreateWithCString", ptr, ptr, ctypes.c_char_p, ctypes.c_uint32)
    owner_pid = ptr.in_dll(quartz, "kCGWindowOwnerPID")
    session = session_info()
    if not session:
        return False
    try:
        # Never infer a windowless browser from a background/non-console session.
        for name, expected in ((b"kCGSSessionOnConsoleKey", ptr.in_dll(foundation, "kCFBooleanTrue").value),
                               (b"kCGSSessionUserIDKey", None)):
            key = make_string(None, name, 0x08000100)  # kCFStringEncodingUTF8
            if not key:
                return False
            try:
                value = get_value(session, key)
            finally:
                release(key)
            if not value:
                return False
            if expected is not None:
                if value != expected:
                    return False
            else:
                uid = ctypes.c_int()
                if type_id(value) != number_type or not number_value(value, 9, ctypes.byref(uid)) or uid.value != os.getuid():
                    return False
    finally:
        release(session)
    windows = window_info(0, 0)  # kCGWindowListOptionAll, kCGNullWindowID
    if not windows:
        return False
    try:
        size = count(windows)
        if size <= 0:  # No positive evidence that this is a working GUI session.
            return False
        owns_surfaces = False
        for i in range(size):
            window = get_item(windows, i)
            owner = get_value(window, owner_pid) if window else None
            value = ctypes.c_int()
            if (not owner or type_id(owner) != number_type
                    or not number_value(owner, 9, ctypes.byref(value))):  # kCFNumberIntType
                return False
            owns_surfaces |= value.value == pid
        # Chrome can keep non-window surfaces in Quartz after its last window closes.
        return not owns_surfaces or chrome_ax_has_no_windows(pid, foundation)
    finally:
        release(windows)


def chrome_ax_has_no_windows(pid, foundation):
    """Resolve ambiguous Quartz surfaces only with an existing accessibility grant."""
    ax = ctypes.CDLL("/System/Library/Frameworks/ApplicationServices.framework/Frameworks/HIServices.framework/HIServices")
    ptr = ctypes.c_void_p
    if not native_function(ax, "AXIsProcessTrusted", ctypes.c_ubyte)():
        return False
    make_app = native_function(ax, "AXUIElementCreateApplication", ptr, ctypes.c_int)
    timeout = native_function(ax, "AXUIElementSetMessagingTimeout", ctypes.c_int, ptr, ctypes.c_float)
    copy = native_function(ax, "AXUIElementCopyAttributeValue", ctypes.c_int, ptr, ptr, ctypes.POINTER(ptr))
    release = native_function(foundation, "CFRelease", None, ptr)
    make_string = native_function(foundation, "CFStringCreateWithCString", ptr, ptr, ctypes.c_char_p, ctypes.c_uint32)
    app = make_app(pid)
    name = make_string(None, b"AXWindows", 0x08000100)
    result = ptr()
    try:
        if not app or not name or timeout(app, 0.5) != 0 or copy(app, name, ctypes.byref(result)) != 0 or not result:
            return False
        type_id = native_function(foundation, "CFGetTypeID", ctypes.c_ulong, ptr)
        array_type = native_function(foundation, "CFArrayGetTypeID", ctypes.c_ulong)
        count = native_function(foundation, "CFArrayGetCount", ctypes.c_long, ptr)
        return type_id(result) == array_type() and count(result) == 0
    finally:
        for value in (result, name, app):
            if value:
                release(value)


def singleton_peer_pid(client):
    return client.getsockopt(0, 2)  # Darwin SOL_LOCAL, LOCAL_PEERPID


def notify_running_chrome(profile, deadline):
    """Ask the existing singleton to load the profile, without starting any process."""
    if sys.platform != "darwin":
        return False
    lock = CHROME / "SingletonLock"
    endpoint = CHROME / "SingletonSocket"
    cookie_path = CHROME / "SingletonCookie"
    lock_target = os.readlink(lock)
    hostname, _, pid_text = lock_target.rpartition("-")
    if hostname != socket.gethostname() or not pid_text.isascii() or not pid_text.isdecimal():
        return False
    pid = int(pid_text)
    if not 0 < pid < 2**31:
        return False
    target = Path(os.readlink(endpoint))
    cookie = os.readlink(cookie_path)
    peer_cookie = target.parent / cookie_path.name
    if not target.is_absolute() or target.name != endpoint.name or not cookie:
        return False
    directory = target.parent.lstat()
    if (not stat.S_ISDIR(directory.st_mode) or directory.st_uid != os.getuid() or directory.st_mode & 0o077
            or not stat.S_ISSOCK(target.lstat().st_mode) or os.readlink(peer_cookie) != cookie):
        return False
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
        client.settimeout(remaining_time(deadline))
        client.connect(str(target))
        if singleton_peer_pid(client) != pid or not (executable := chrome_executable(pid)):
            return False
        if (os.readlink(lock) != lock_target or Path(os.readlink(endpoint)) != target
                or os.readlink(cookie_path) != cookie or os.readlink(peer_cookie) != cookie):
            return False
        # Chromium ProcessSingleton's POSIX notification format: no URL, no launch,
        # no user interaction, and no fallback if Chrome is gone or will not ACK.
        argv = ("START", str(CHROME), str(executable), f"--profile-directory={profile}", "--no-startup-window")
        if chrome_has_no_windows(pid) is not True:
            return False
        client.settimeout(remaining_time(deadline))
        client.sendall(b"\0".join(map(os.fsencode, argv)))
        reply = bytearray()
        while len(reply) <= 3:
            client.settimeout(remaining_time(deadline))
            part = client.recv(4 - len(reply))
            if not part:
                return reply == b"ACK"
            reply.extend(part)
        return False


@contextmanager
def woken_client(deadline):
    if sys.platform != "darwin":
        yield None
        return
    try:
        profile = wake_profile()
    except (AttributeError, IndexError, KeyError, OSError, RecursionError, SyntaxError, TypeError, ValueError):
        profile = None
    if profile is not None:
        # A host may have already connected while checking on-disk setup.
        with connected_client(deadline) as client:
            if client is not None:
                yield client
                return
        reconnect_deadline = min(deadline, time.monotonic() + WAKE_WAIT)
        try:
            woken = notify_running_chrome(profile, reconnect_deadline)
        except (AttributeError, OSError, TypeError, ValueError):
            woken = False
        if woken:
            while time.monotonic() < reconnect_deadline:
                with connected_client(reconnect_deadline) as client:
                    if client is not None:
                        yield client
                        return
                time.sleep(min(WAKE_POLL, max(0, reconnect_deadline - time.monotonic())))
    yield None


def choose_profile(selected=None):
    names = discover_profiles()
    if not names:
        if selected not in (None, "Default"):
            raise ValueError("No matching Chrome profile; the only available profile is Default")
        return "Default", "Default"
    profiles = list(names)
    labels = {key: f"{name!r} ({key!r})" if list(names.values()).count(name) > 1 else repr(name)
              for key, name in names.items()}
    if selected in labels:
        return selected, labels[selected]
    if selected is None and len(profiles) == 1:
        key = profiles[0]
        return key, labels[key]
    for index, key in enumerate(profiles, 1):
        # Escape control characters from the profile name before writing to a terminal.
        print(f"  {index}. {labels[key]} — profile directory {key!r}", file=sys.stderr)
    if selected is not None:
        raise ValueError("Unknown Chrome profile directory; choose one of the listed profile directories")
    if not sys.stdin.isatty() or not sys.stderr.isatty():
        raise ValueError("Multiple Chrome profiles found. Ask the user which profile directory to use. No changes made.")
    try:
        print("Choose a Chrome profile number: ", end="", file=sys.stderr, flush=True)
        choice = int(input())
        if 1 <= choice <= len(profiles):
            key = profiles[choice - 1]
            return key, labels[key]
    except (EOFError, ValueError):
        pass
    raise ValueError("No valid Chrome profile selected; no changes made")


def atomic_write(path, content, mode):
    with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as stream:
        temporary = Path(stream.name)
        try:
            stream.write(content)
            stream.close()
            temporary.chmod(mode)
            temporary.replace(path)
        finally:
            if temporary.exists():
                temporary.unlink()


def native_launcher(python, bridge):
    return f'#!/bin/sh\nexec {shlex.quote(str(python))} -I {shlex.quote(str(bridge))} host "${{1-}}"\n'.encode()


def install(selected=None, *, open_settings=False, timeout=None):
    """Stage bundled files without touching Chrome preferences; caller must obtain consent.

    Raises ValueError for profile selection and OSError for staging. Chrome is never launched.
    """
    if open_settings:
        raise ValueError("Launching Chrome is not supported")
    source = Path(__file__).resolve().parent
    _, label = choose_profile(selected)
    extension = INSTALL / "extension"
    files = {name: (source / "extension" / name).read_bytes() for name in ("manifest.json", "background.js")}
    files["read-page.js"] = (source.parent / "extension/shared/read-page.js").read_bytes()
    extension.mkdir(parents=True, exist_ok=True)
    for filename in ("read-page.js", "background.js", "manifest.json"):
        atomic_write(extension / filename, files[filename], 0o644)
    atomic_write(INSTALL / "bridge.py", (source / "bridge.py").read_bytes(), 0o644)
    launcher = INSTALL / "native-host"
    atomic_write(launcher, native_launcher(Path(sys.executable).absolute(), INSTALL / "bridge.py"), 0o755)
    destination = host_manifest()
    destination.parent.mkdir(parents=True, exist_ok=True)
    manifest = {"name": HOST, "description": "agent-browser local Chrome extension transport",
                "path": str(launcher), "type": "stdio", "allowed_origins": [extension_origin(extension)]}
    atomic_write(destination, (json.dumps(manifest, indent=2) + "\n").encode(), 0o644)

    print(f"Finish setup in Chrome profile {label}.")
    print("Go to chrome://extensions/ in that profile.")
    print(loading_instructions(), flush=True)
    print("Setup is pending until Chrome loads and connects the extension. Check its status using the setup service.")

def main():
    try:
        if len(sys.argv) != 3 or sys.argv[1] != "host":
            raise ValueError("Only native host startup is supported")
        native_host(sys.argv[2])
    except (OSError, ValueError) as error:
        print(f"agent-browser: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
