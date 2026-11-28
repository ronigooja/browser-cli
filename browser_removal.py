"""Conservative, reversible cleanup after the caller confirms extension removal.

Only exact current Chrome installation contents and registration are eligible.
Safari app ownership cannot be established from its bundle identifier or an
arbitrary valid signature, so this module never moves it. No profiles, app
group data, settings, or browser directories are removed.
"""

import base64
import hashlib
import json
import os
from pathlib import Path
import secrets
import shlex
import stat


HOST = "com.agent_browser.background"
SOURCE = Path(__file__).resolve().parent


def _check_path(path, home):
    """Reject symlinks and non-owned components from home through path."""
    path.relative_to(home)
    current = home
    for part in ("", *path.relative_to(home).parts):
        if part:
            current = current / part
        info = current.lstat()
        if stat.S_ISLNK(info.st_mode) or info.st_uid != os.getuid():
            raise ValueError(f"Unsafe path: {current}")


def _origin():
    manifest = json.loads((SOURCE / "chrome-background/extension/manifest.json").read_text())
    digest = hashlib.sha256(base64.b64decode(manifest["key"], validate=True)).hexdigest()[:32]
    return "chrome-extension://" + digest.translate(str.maketrans("0123456789abcdef", "abcdefghijklmnop")) + "/"


def _validate_install(path, home):
    _check_path(path, home)
    expected = {
        "bridge.py": SOURCE / "chrome-background/bridge.py",
        "extension/manifest.json": SOURCE / "chrome-background/extension/manifest.json",
        "extension/background.js": SOURCE / "chrome-background/extension/background.js",
        "extension/read-page.js": SOURCE / "extension/shared/read-page.js",
    }
    if not path.is_dir() or {p.name for p in path.iterdir()} != {"bridge.py", "native-host", "extension"}:
        raise ValueError("Unexpected Chrome installation contents")
    extension = path / "extension"
    _check_path(extension, home)
    if not extension.is_dir() or {p.name for p in extension.iterdir()} != {"manifest.json", "background.js", "read-page.js"}:
        raise ValueError("Unexpected Chrome extension contents")
    for name, source in expected.items():
        target = path / name
        _check_path(target, home)
        if not target.is_file() or target.read_bytes() != source.read_bytes():
            raise ValueError(f"Unrecognized installation file: {target}")
    launcher = path / "native-host"
    _check_path(launcher, home)
    if not launcher.is_file():
        raise ValueError("Invalid native host launcher")
    text = launcher.read_text()
    lines = text.splitlines()
    if len(lines) != 2 or lines[0] != "#!/bin/sh" or not lines[1].endswith(' host "${1-}"'):
        raise ValueError("Unrecognized native host launcher")
    args = shlex.split(lines[1][:-len(' host "${1-}"')])
    if len(args) != 4 or args[0] != "exec" or not Path(args[1]).is_absolute() or args[2:] != ["-I", str(path / "bridge.py")]:
        raise ValueError("Unrecognized native host launcher")


def _validate_registration(path, install, home):
    _check_path(path, home)
    if not path.is_file():
        raise ValueError("Invalid native host registration")
    registration = json.loads(path.read_text())
    if registration != {"name": HOST, "description": "agent-browser local Chrome extension transport",
                        "path": str(install / "native-host"), "type": "stdio", "allowed_origins": [_origin()]}:
        raise ValueError("Unrecognized native host registration")


def _trash(home):
    trash = home / ".Trash"
    _check_path(trash, home)
    info = trash.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_mode & 0o077:
        raise ValueError("Trash is not a private directory")
    destination = trash / ("agent-browser-removal-" + secrets.token_hex(16))
    destination.mkdir(mode=0o700)
    return destination


def cleanup(browser):
    """Return moved paths and errors; caller must first obtain explicit consent.

    A partial result means only the listed paths were moved. Trash paths are
    returned for recovery. This function does not verify browser UI removal.
    """
    result = {"browser": browser, "moved": [], "errors": []}
    if browser == "safari":
        result["errors"].append("Safari app retained: a trusted signing identity cannot be independently established; custom binaries are never moved.")
        return result
    if browser != "chrome":
        result["errors"].append("Unsupported browser")
        return result
    home = Path.home()
    install = home / "Library/Application Support/agent-browser/chrome-background"
    registration = home / "Library/Application Support/Google/Chrome/NativeMessagingHosts" / (HOST + ".json")
    candidates = []
    for path, validate in ((install, lambda: _validate_install(install, home)),
                           (registration, lambda: _validate_registration(registration, install, home))):
        try:
            validate()
            candidates.append((path, validate))
        except FileNotFoundError:
            # A broken symlink must not be mistaken for absence.
            if os.path.lexists(path):
                result["errors"].append(f"Unsafe or incomplete path: {path}")
        except (OSError, ValueError, TypeError, KeyError) as error:
            result["errors"].append(f"Retained {path}: {error}")
    if not candidates:
        return result
    try:
        destination = _trash(home)
    except (OSError, ValueError) as error:
        result["errors"].append(f"Cannot create private Trash destination: {error}")
        return result
    for index, (path, validate) in enumerate(candidates):
        try:
            validate()
            target = destination / (str(index) + "-" + path.name)
            os.rename(path, target)
            result["moved"].append({"from": str(path), "to": str(target)})
        except (OSError, ValueError, TypeError, KeyError) as error:
            result["errors"].append(f"Retained {path}: {error}")
    return result
