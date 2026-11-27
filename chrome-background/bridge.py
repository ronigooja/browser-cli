"""Chrome native host, background-open client, and macOS installer."""

import base64
from contextlib import ExitStack, contextmanager
import hashlib
import json
import os
from pathlib import Path
import select
import shlex
import signal
import socket
import stat
import struct
import subprocess
import sys
import tempfile
import time
from urllib.parse import urlsplit


HOST = "com.browser_cli.background"
CHROME = Path.home() / "Library/Application Support/Google/Chrome"
INSTALL = Path.home() / "Library/Application Support/browser-cli/chrome-background"
SETUP = f"{shlex.quote(os.environ.get('BROWSER_CLI_PATH', './browser-cli'))} --browser chrome install-background"
SETUP_WAIT = 120
LIMIT = 65536


def runtime_dir():
    # macOS limits Unix socket paths to 103 bytes, regardless of the home directory.
    home = hashlib.sha256(os.fsencode(Path.home())).hexdigest()[:12]
    directory = Path(f"/tmp/browser-cli-{os.getuid()}-{home}")
    directory.mkdir(mode=0o700, exist_ok=True)
    info = directory.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
        raise ValueError("Unsafe browser-cli socket directory")
    return directory


def frame(message):
    data = json.dumps(message).encode("utf-8")
    if len(data) > LIMIT:
        raise ValueError("Chrome background message is too large")
    return struct.pack("=I", len(data)) + data


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
        raise ValueError("Invalid Chrome background message size")
    result = json.loads(exact(size))
    if not isinstance(result, dict):
        raise ValueError("Invalid Chrome background message")
    return result


def validate_url(url):
    if not isinstance(url, str) or "\\" in url or any(ord(c) <= 32 or ord(c) == 127 for c in url):
        raise ValueError("Chrome background open requires an HTTP(S) URL without backslashes, spaces or control characters")
    parsed = urlsplit(url)
    if parsed.scheme not in ("http", "https") or not parsed.hostname or not url.lower().startswith(("http://", "https://")):
        raise ValueError("Chrome background open requires an HTTP(S) URL")
    if parsed.username is not None or parsed.password is not None:
        raise ValueError("URLs with embedded credentials are not supported")


def serve(listener):
    while True:
        # A readable native stdin with no request in flight indicates disconnection.
        ready, _, _ = select.select([0, listener], [], [])
        if 0 in ready:
            return
        client, _ = listener.accept()
        with client:
            try:
                request = receive(client.fileno(), time.monotonic() + 1)
            except (EOFError, ValueError, TimeoutError, OSError):
                continue

            try:
                url = request.get("url")
                validate_url(url)
                expires = request.get("expires")
                remaining = (expires - int(time.time() * 1000)) / 1000 if type(expires) is int else 0
                if not 0 < remaining <= 86400:
                    raise ValueError("Invalid or expired Chrome background deadline")
                request_bytes = frame({"url": url, "expires": expires})
            except ValueError as error:
                reply = {"error": str(error)}
            else:
                # A missing reply terminates this connection; a late reply cannot
                # be mistaken for the next operation. The extension reconnects.
                sys.stdout.buffer.write(request_bytes)
                sys.stdout.buffer.flush()
                reply = receive(0, time.monotonic() + remaining)

            try:
                client.settimeout(1)
                client.sendall(frame(reply))
            except (OSError, ValueError):
                pass


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
            except (EOFError, TimeoutError, BrokenPipeError):
                pass


@contextmanager
def connected_client(deadline):
    with ExitStack() as stack:
        connected = None
        for endpoint in sorted(runtime_dir().glob("host-*/socket")):
            if time.monotonic() >= deadline:
                raise TimeoutError
            client = stack.enter_context(socket.socket(socket.AF_UNIX, socket.SOCK_STREAM))
            client.settimeout(max(0.001, deadline - time.monotonic()))
            try:
                client.connect(str(endpoint))
            except (FileNotFoundError, ConnectionRefusedError):
                client.close()
                continue
            if connected is not None:
                raise ValueError("The browser-cli extension is connected in more than one Chrome profile. Leave it enabled only in the profile you want to use.")
            connected = client
        yield connected


def background_open(url, timeout):
    validate_url(url)
    if not 0 < timeout <= 86400:
        raise ValueError("Chrome background timeout must be at most 86400 seconds")
    deadline = time.monotonic() + timeout
    request = frame({"url": url, "expires": int((time.time() + timeout) * 1000)})
    try:
        with connected_client(deadline) as client:
            if client is not None:
                if time.monotonic() >= deadline:
                    raise TimeoutError
                client.settimeout(max(0.001, deadline - time.monotonic()))
                client.sendall(request)
                reply = receive(client.fileno(), deadline)
                if "error" in reply:
                    raise ValueError(str(reply["error"]))
                if type(reply.get("id")) is not int or reply["id"] < 0 or reply.get("action") not in ("created", "reused"):
                    raise ValueError("Unexpected reply from Chrome background extension")
                print(f'id:{reply["id"]}\t{reply["action"]}')
                return 0
    except (TimeoutError, EOFError, BrokenPipeError, ConnectionResetError):
        raise ValueError("Chrome background connection timed out or closed; check Chrome before retrying (a tab may already have opened)") from None
    return offer_setup()


def extension_origin(directory):
    manifest = json.loads((directory / "manifest.json").read_text(encoding="utf-8"))
    digest = hashlib.sha256(base64.b64decode(manifest["key"], validate=True)).hexdigest()[:32]
    identifier = digest.translate(str.maketrans("0123456789abcdef", "abcdefghijklmnop"))
    return f"chrome-extension://{identifier}/"


def host_manifest():
    return CHROME / "NativeMessagingHosts" / f"{HOST}.json"


def setup_state():
    # Chrome records unpacked extensions in either profile preferences file.
    # A missing entry is not proof that an extension is absent (e.g. custom profiles).
    source = Path(__file__).resolve().parent / "extension"
    identifier = extension_origin(source).split("/")[2]
    for filename in ("Preferences", "Secure Preferences"):
        for path in CHROME.glob(f"*/{filename}"):
            try:
                preferences = json.loads(path.read_text(encoding="utf-8"))
                if identifier in preferences.get("extensions", {}).get("settings", {}):
                    return "loaded"
            except (OSError, ValueError, AttributeError, TypeError):
                continue
    if host_manifest().is_file() and (INSTALL / "extension/manifest.json").is_file():
        return "pending"
    return "missing"


def require_connection(state):
    if state == "loaded":
        raise ValueError(f"The browser-cli extension is installed but not connected. Open a regular Chrome window in the profile where it is installed and make sure it is enabled at chrome://extensions/. Then run `{SETUP} --check`.")
    if state == "pending":
        raise ValueError(f"Chrome background setup is not complete. In the intended Chrome profile, open chrome://extensions/, turn on Developer mode, then click Load unpacked and select {INSTALL / 'extension'}. If already loaded, enable or reload it. Then run `{SETUP} --check`.")
    raise ValueError(f"Chrome background mode requires the browser-cli extension. Ask the user whether to set it up. Only after they confirm, run `{SETUP}`.")


def offer_setup():
    state = setup_state()
    if state != "missing" or not sys.stdin.isatty() or not sys.stderr.isatty():
        require_connection(state)
    print("Chrome background mode requires the browser-cli extension. Set it up now? [y/N] ",
          end="", file=sys.stderr, flush=True)
    try:
        confirmed = input().strip().lower() in ("y", "yes")
    except EOFError:
        confirmed = False
    if not confirmed:
        raise ValueError("Setup declined; no changes made")
    install()
    print("The original page was not opened.", file=sys.stderr)
    return 1


def check_setup(wait=0):
    deadline = time.monotonic() + wait
    while True:
        with connected_client(time.monotonic() + 1) as client:
            if client is not None:
                print("Chrome background setup complete. You can retry open --background.")
                return True
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return False
        time.sleep(min(1, remaining))


def choose_profile():
    try:
        state = json.loads((CHROME / "Local State").read_text(encoding="utf-8"))
    except FileNotFoundError:
        state = {}
    profile = state.get("profile", {})
    cache = profile.get("info_cache", {})
    profiles = [key for key, info in cache.items()
                if key not in (".", "..", "System Profile", "Guest Profile")
                and Path(key).name == key and (CHROME / key).is_dir()
                and not info.get("is_omitted")]
    if not profiles:
        return "Default", "Default"
    names = {key: cache[key].get("name") or key for key in profiles}
    labels = {key: f"{name!r} ({key!r})" if list(names.values()).count(name) > 1 else repr(name)
              for key, name in names.items()}
    if len(profiles) == 1:
        key = profiles[0]
        return key, labels[key]
    profiles.sort(key=lambda key: (key != profile.get("last_used"), key))
    for index, key in enumerate(profiles, 1):
        # Escape control characters from the profile name before writing to a terminal.
        print(f"  {index}. {labels[key]}", file=sys.stderr)
    if not sys.stdin.isatty():
        raise ValueError("Multiple Chrome profiles found; rerun this command in an interactive terminal to choose one")
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


def install():
    source = Path(__file__).resolve().parent
    profile, label = choose_profile()
    extension = INSTALL / "extension"
    extension.mkdir(parents=True, exist_ok=True)
    for filename in ("manifest.json", "background.js"):
        atomic_write(extension / filename, (source / "extension" / filename).read_bytes(), 0o644)
    atomic_write(INSTALL / "bridge.py", (source / "bridge.py").read_bytes(), 0o644)
    launcher = INSTALL / "native-host"
    python = shlex.quote(str(Path(sys.executable).absolute()))
    bridge = shlex.quote(str(INSTALL / "bridge.py"))
    atomic_write(launcher, f'#!/bin/sh\nexec {python} -I {bridge} host "${{1-}}"\n'.encode(), 0o755)
    destination = host_manifest()
    destination.parent.mkdir(parents=True, exist_ok=True)
    manifest = {"name": HOST, "description": "browser-cli Chrome background tabs",
                "path": str(launcher), "type": "stdio", "allowed_origins": [extension_origin(extension)]}
    atomic_write(destination, (json.dumps(manifest, indent=2) + "\n").encode(), 0o644)

    print(f"Finish setup in Chrome profile {label}.")
    print("If the extensions page does not open in that profile, go to chrome://extensions/ there.")
    print("Turn on Developer mode, click Load unpacked, and select this folder. If already loaded, click Reload instead.")
    print(extension, flush=True)
    subprocess.run(["/usr/bin/open", "-na", "Google Chrome", "--args",
                    f"--profile-directory={profile}", "chrome://extensions/"], check=True)
    if sys.stdin.isatty() and sys.stderr.isatty():
        print(f"Waiting up to {SETUP_WAIT} seconds for the extension to connect…", flush=True)
        if check_setup(SETUP_WAIT):
            return
    print(f"Setup is pending until Chrome loads and connects the extension. After loading it, run `{SETUP} --check`.")


def main():
    command, *args = sys.argv[1:]
    try:
        if command == "install":
            install()
        elif command == "check":
            if not check_setup():
                require_connection(setup_state())
        elif command == "host":
            native_host(*args)
        elif command == "open":
            return background_open(args[0], int(args[1]))
    except (OSError, ValueError, subprocess.CalledProcessError) as error:
        print(f"browser-cli: {error}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("\nbrowser-cli: interrupted", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
