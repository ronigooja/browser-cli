"""Consent-gated extension setup, separate from all page reads."""

import time

from browser_policy import BROWSERS, clipped, result, token_valid
from browser_service import Capabilities, blocker


GRANT_SECONDS = 300
PROFILE_LIMIT = 50


def profile_directory(value):
    return (isinstance(value, str) and 0 < len(value) <= 255 and not value.startswith("-")
            and value not in (".", "..") and value.isprintable() and "/" not in value and "\\" not in value)


class Setup:
    def __init__(self, service, clock=time.monotonic):
        self.service = service
        self.grants = Capabilities(clock, GRANT_SECONDS, 64)

    def call(self, args, scope):
        allowed = {"check": set(), "prepare": {"approved"}, "install": {"grant", "profile"}}
        if not isinstance(args, dict) or args.get("browser") not in BROWSERS:
            raise ValueError("Choose safari or chrome for setup_extension.")
        browser, action = args["browser"], args.get("action")
        if not isinstance(action, str) or action not in allowed or args.keys() - allowed[action] - {"action", "browser"}:
            raise ValueError("Choose check, prepare, or install with the appropriate setup arguments.")
        if browser == "safari" and (action == "install" or "approved" in args):
            raise ValueError("Safari setup is manual; use check for production setup guidance.")
        if action == "prepare" and type(args.get("approved", False)) is not bool:
            raise ValueError("approved must be a boolean.")
        if action == "install" and (not token_valid(args.get("grant")) or (
                "profile" in args and not profile_directory(args["profile"]))):
            raise ValueError("Use the grant and profile returned by prepare.")
        try:
            if action == "check" or browser == "safari":
                return self.check(browser)
            if action == "prepare":
                return self.prepare(args.get("approved", False), scope)
            return self.install(args["grant"], args.get("profile"), scope)
        except (OSError, EOFError, ValueError, TypeError, RuntimeError, KeyError, AttributeError):
            return result("setup_error", browser, "Setup could not finish. Check the connection before making any further changes.", next_action="check")

    def pending(self, backend, profile=None):
        instructions = backend.loading_instructions()
        if not isinstance(instructions, str) or len(instructions) > 4096:
            raise ValueError("Invalid loading instructions")
        return result("extension_pending", "chrome", "Finish loading the extension in Chrome; do not reinstall.",
                      instructions=("In the intended regular Chrome profile, verify the profile is correct. "
                                    "If the extensions page did not open there, switch to that profile and go to chrome://extensions/.\n"
                                    f"{instructions}\nAfter loading it, use setup_extension with action=check."),
                      next_action="check", **({"profile": profile} if profile else {}))

    def check(self, browser):
        reply = self.service.request(browser, "status")
        state = reply["status"]
        if state == "ok":
            return result("extension_connected", browser, "The browser extension is connected.")
        if browser == "safari" and state in ("extension_required", "extension_pending", "extension_disconnected", "permission_required"):
            return result(state, browser, "Use the production Safari extension, not the development probe.",
                          instructions=("In Safari > Settings > Extensions, enable Agent Browser once and allow its website access on all websites once. "
                                        "Afterward, use setup_extension with action=check. No per-site permission or automatic install is offered."),
                          next_action="check")
        if state == "extension_pending":
            return self.pending(self.service.backends[browser])
        if state == "extension_disconnected" and reply.get("update_required") is not True:
            return result(state, browser, "Do not reinstall. In the intended regular Chrome profile, open a regular window and ensure Agent Browser is enabled at chrome://extensions/. Keep it enabled in only one profile and check again.",
                          next_action="check")
        output = blocker(browser, reply)
        if state in ("extension_required", "extension_disconnected"):
            output["next_action"] = "prepare" if state == "extension_required" else "check"
        return output

    def profiles(self):
        values = self.service.backends["chrome"].profiles()
        if (not isinstance(values, dict) or not 1 <= len(values) <= PROFILE_LIMIT or
                not all(profile_directory(key) and isinstance(label, str) for key, label in values.items())):
            raise ValueError("Cannot verify regular Chrome profiles")
        return dict(values)

    @staticmethod
    def choices(profiles):
        return [{"directory": key, "name": clipped(name, 200)} for key, name in profiles.items()]

    def prepare(self, approved, scope):
        self.grants.revoke_scope(scope)
        if not approved:
            return result("consent_required", "chrome", "Ask the user before staging the extension files and registering the local native messaging host. The user loads the extension themselves; setup does not open the requested page.",
                          next_action="prepare")
        state = self.check("chrome")
        if state["status"] != "extension_required":
            return state
        profiles = self.profiles()
        grant = self.grants.issue(scope, profiles)
        if not grant:
            return result("setup_error", "chrome", "Too many outstanding setup approvals. No changes were made.")
        multi = len(profiles) > 1
        return result("profile_required" if multi else "install_ready", "chrome",
                      "Ask which regular Chrome profile to use. Do not guess." if multi else "Use the only listed Chrome profile.",
                      profiles=self.choices(profiles), grant=grant, expires_in_seconds=GRANT_SECONDS, next_action="install")

    def install(self, grant, profile, scope):
        missing = result("grant_required", "chrome", "This session has no valid grant. Obtain fresh user approval before preparing again.", next_action="prepare")
        profiles = self.grants.get(grant, scope)
        if profiles is None:
            return missing
        if profile is None and len(profiles) > 1:
            return result("profile_required", "chrome", "Ask which listed Chrome profile to use before installing.",
                          profiles=self.choices(profiles), next_action="install")
        profile = profile if profile is not None else next(iter(profiles))
        if profile not in profiles:
            return result("profile_invalid", "chrome", "Use one of the regular Chrome profiles returned by prepare.", next_action="install")
        # A valid install attempt consumes consent even if the subsequent preflight fails.
        self.grants.discard(grant)
        state = self.check("chrome")
        if state["status"] != "extension_required":
            return state
        if self.profiles() != profiles:
            return result("profiles_changed", "chrome", "Chrome profiles changed. Ask for fresh approval and profile selection.", next_action="prepare")
        backend = self.service.backends["chrome"]
        backend.install(profile)
        return self.pending(backend, self.choices({profile: profiles[profile]})[0])
