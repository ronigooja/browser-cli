"""Chrome native host, background-open client, and macOS installer."""

import base64
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
SETUP = "browser-cli --browser chrome install-background"
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


def background_open(url, timeout):
    validate_url(url)
    if not 0 < timeout <= 86400:
        raise ValueError("Chrome background timeout must be at most 86400 seconds")
    deadline = time.monotonic() + timeout
    request = frame({"url": url, "expires": int((time.time() + timeout) * 1000)})
    clients = []
    try:
        for endpoint in sorted(runtime_dir().glob("host-*/socket")):
            if time.monotonic() >= deadline:
                raise TimeoutError
            client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            clients.append(client)
            client.settimeout(max(0.001, deadline - time.monotonic()))
            try:
                client.connect(str(endpoint))
            except (FileNotFoundError, ConnectionRefusedError):
                clients.pop().close()
                continue
            if len(clients) > 1:
                raise ValueError("Multiple Chrome extension instances are connected; disable the extension in the other profiles")
        if not clients:
            raise ValueError(f"Chrome background extension is not connected; run `{SETUP}` or check it in chrome://extensions/")
        if time.monotonic() >= deadline:
            raise TimeoutError
        client = clients[0]
        client.settimeout(max(0.001, deadline - time.monotonic()))
        client.sendall(request)
        reply = receive(client.fileno(), deadline)
        if "error" in reply:
            raise ValueError(str(reply["error"]))
        if type(reply.get("id")) is not int or reply["id"] < 0 or reply.get("action") not in ("created", "reused"):
            raise ValueError("Unexpected reply from Chrome background extension")
        print(f'id:{reply["id"]}\t{reply["action"]}')
    except (TimeoutError, EOFError, BrokenPipeError, ConnectionResetError):
        raise ValueError("Chrome background connection timed out or closed; check Chrome before retrying (a tab may already have opened)") from None
    finally:
        for client in clients:
            client.close()


def extension_origin(directory):
    manifest = json.loads((directory / "manifest.json").read_text(encoding="utf-8"))
    digest = hashlib.sha256(base64.b64decode(manifest["key"], validate=True)).hexdigest()[:32]
    identifier = digest.translate(str.maketrans("0123456789abcdef", "abcdefghijklmnop"))
    return f"chrome-extension://{identifier}/"


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
        return "Default"
    if len(profiles) == 1:
        return profiles[0]
    profiles.sort(key=lambda key: (key != profile.get("last_used"), key))
    for index, key in enumerate(profiles, 1):
        name = cache[key].get("name") or key
        # Escape control characters from the profile name before writing to a terminal.
        print(f"  {index}. {name!r} ({key!r})", file=sys.stderr)
    if not sys.stdin.isatty():
        raise ValueError("Multiple Chrome profiles found; rerun this command in an interactive terminal to choose one")
    try:
        print("Choose a Chrome profile number: ", end="", file=sys.stderr, flush=True)
        choice = int(input())
        if 1 <= choice <= len(profiles):
            return profiles[choice - 1]
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
    profile = choose_profile()
    extension = INSTALL / "extension"
    extension.mkdir(parents=True, exist_ok=True)
    for filename in ("manifest.json", "background.js"):
        atomic_write(extension / filename, (source / "extension" / filename).read_bytes(), 0o644)
    atomic_write(INSTALL / "bridge.py", (source / "bridge.py").read_bytes(), 0o644)
    launcher = INSTALL / "native-host"
    python = shlex.quote(str(Path(sys.executable).absolute()))
    bridge = shlex.quote(str(INSTALL / "bridge.py"))
    atomic_write(launcher, f'#!/bin/sh\nexec {python} -I {bridge} host "${{1-}}"\n'.encode(), 0o755)
    manifests = CHROME / "NativeMessagingHosts"
    manifests.mkdir(parents=True, exist_ok=True)
    manifest = {"name": HOST, "description": "browser-cli Chrome background tabs",
                "path": str(launcher), "type": "stdio", "allowed_origins": [extension_origin(extension)]}
    atomic_write(manifests / f"{HOST}.json", (json.dumps(manifest, indent=2) + "\n").encode(), 0o644)

    print(f"Bridge installed. In Chrome profile {profile!r}, open chrome://extensions/.")
    print("Enable Developer mode, then click Load unpacked and choose the directory below. If already installed, click Reload instead.")
    print(extension, flush=True)
    subprocess.run(["/usr/bin/open", "-na", "Google Chrome", "--args",
                    f"--profile-directory={profile}", "chrome://extensions/"], check=True)


def main():
    command, *args = sys.argv[1:]
    try:
        if command == "install":
            install()
        elif command == "host":
            native_host(*args)
        elif command == "open":
            background_open(args[0], int(args[1]))
    except (OSError, ValueError, subprocess.CalledProcessError) as error:
        print(f"browser-cli: {error}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("\nbrowser-cli: interrupted", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
