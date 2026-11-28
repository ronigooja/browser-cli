"""Consent-gated extension setup, separate from all page reads."""

import time

from browser_policy import BROWSERS, clipped, result, token_valid
from browser_removal import cleanup as cleanup_installation
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
        self.staged_update = None

    def call(self, args, scope):
        allowed = {"check": set(), "prepare": {"approved", "update"}, "install": {"grant", "profile"},
                   "uninstall": {"approved"}, "cleanup_uninstall": {"approved", "removal_confirmed"}}
        if not isinstance(args, dict) or args.get("browser") not in BROWSERS:
            raise ValueError("Choose safari or chrome for setup_extension.")
        browser, action = args["browser"], args.get("action")
        if not isinstance(action, str) or action not in allowed or args.keys() - allowed[action] - {"action", "browser"}:
            raise ValueError("Choose check, prepare, install, uninstall, or cleanup_uninstall with the appropriate arguments.")
        if browser == "safari" and action not in ("check", "uninstall", "cleanup_uninstall") and (action == "install" or {"approved", "update"} & args.keys()):
            raise ValueError("Safari setup is manual; use check for production setup guidance.")
        if action == "prepare" and any(type(args.get(key, False)) is not bool for key in ("approved", "update")):
            raise ValueError("approved and update must be booleans.")
        if action == "install" and (not token_valid(args.get("grant")) or (
                "profile" in args and not profile_directory(args["profile"]))):
            raise ValueError("Use the grant and profile returned by prepare.")
        if action in ("uninstall", "cleanup_uninstall") and any(
                type(args.get(key, False)) is not bool for key in ("approved", "removal_confirmed")):
            raise ValueError("Approval and removal confirmation must be booleans.")
        try:
            if action == "cleanup_uninstall":
                return self.cleanup_uninstall(browser, args.get("approved", False), args.get("removal_confirmed", False))
            if action == "uninstall":
                return self.uninstall(browser, args.get("approved", False))
            if action == "check" or browser == "safari":
                return self.check(browser)
            if action == "prepare":
                return self.prepare(args.get("approved", False), args.get("update", False), scope)
            return self.install(args["grant"], args.get("profile"), scope)
        except (OSError, EOFError, ValueError, TypeError, RuntimeError, KeyError, AttributeError):
            return result("setup_error", browser, "Setup could not finish. Check the connection before making any further changes.", next_action="check")

    def uninstall(self, browser, approved):
        if not approved:
            return result("consent_required", browser, "Ask before uninstalling Agent Browser. Identify the intended browser profile first.")
        if browser == "safari":
            return result("manual_uninstall_required", browser,
                          "In Safari > Settings > Extensions, select Agent Browser and click Uninstall. No local files or browser settings were changed.")
        reply = self.service.request(browser, "uninstall")
        status = reply["status"]
        if status in ("ok", "uninstall_requested", "uninstall_unverified", "uninstall_unconfirmed", "action_unconfirmed", "timeout", "browser_error"):
            return result("uninstall_unconfirmed", browser,
                          "Chrome removal could not be verified. Check Agent Browser is gone from the intended profile at chrome://extensions/. Do not retry automatically. Native host files were retained.")
        if status == "uninstall_unavailable":
            return result(status, browser, "Chrome did not offer self-removal. Remove Agent Browser manually at chrome://extensions/ in the intended profile.")
        return blocker(browser, reply)

    def cleanup_uninstall(self, browser, approved, confirmed):
        if not (approved and confirmed):
            return result("consent_required", browser, "Ask the user to confirm they removed Agent Browser and approve moving its local files to Trash. For Chrome, check every profile that had the extension.")
        if browser == "chrome":
            reply = self.service.request(browser, "status")
            if reply["status"] not in ("extension_disconnected", "extension_pending", "extension_required") or reply.get("update_required") is True:
                return result("cleanup_refused", browser, "The current Chrome connection state does not permit cleanup. No files were moved.")
        outcome = cleanup_installation(browser)
        moved, errors = outcome["moved"], outcome["errors"]
        status = "cleanup_partial" if moved and errors else "cleanup_refused" if errors else "cleanup_complete"
        return result(status, browser, moved=moved, errors=errors)

    def pending(self, backend, profile=None, update=False):
        instructions = backend.loading_instructions()
        if not isinstance(instructions, str) or len(instructions) > 4096:
            raise ValueError("Invalid loading instructions")
        action = ("Reload the existing Agent Browser extension at chrome://extensions/ to finish updating; do not load a second copy.\n"
                  if update else "")
        return result("extension_pending", "chrome", "Finish loading the extension in Chrome; do not reinstall.",
                      instructions=("In the intended regular Chrome profile, verify the profile is correct. "
                                    "If the extensions page did not open there, switch to that profile and go to chrome://extensions/.\n"
                                     f"{action}{instructions}\n"
                                     "In the extension's Details, make sure Site access is set to On all sites once. "
                                     "Do not make per-site grants. After loading or reloading it, use setup_extension with action=check."),
                      next_action="check", **({"profile": profile} if profile else {}))

    def check(self, browser):
        reply = self.service.request(browser, "status")
        state = reply["status"]
        if state == "ok":
            if browser == "chrome":
                self.staged_update = None
            return result("extension_connected", browser, "The browser extension is connected.")
        if browser == "chrome" and state == "permission_required":
            return result(state, browser, "Open the Agent Browser extension's Details and set Site access to On all sites once. Then check the connection again.",
                          next_action="check")
        if browser == "safari" and reply.get("update_required") is True:
            return dict(blocker(browser, reply), next_action="check")
        if browser == "safari" and state in ("extension_required", "extension_pending", "extension_disconnected", "permission_required", "profile_required"):
            return result(state, browser, "Use the production Safari extension, not the development probe.",
                          instructions=("Open the signed AgentBrowserSafari app once. In Safari > Settings > Extensions, enable Agent Browser "
                                        "in the intended named profile only and leave private access off. In Safari settings, grant all HTTP(S) access once. "
                                        "Enable background requests in the Agent Browser Safari Setup popup. Set AGENT_BROWSER_SAFARI_PROFILE in the MCP environment to the UUID shown there. "
                                        "Then use setup_extension with action=check. No per-site permission or automatic install is offered."),
                          next_action="check")
        if browser == "chrome" and self.staged_update and state in ("extension_pending", "extension_disconnected"):
            return self.pending(self.service.backends[browser], self.staged_update, update=True)
        if state == "extension_pending":
            return self.pending(self.service.backends[browser])
        if state == "extension_disconnected" and reply.get("update_required") is not True:
            return result(state, browser, "Do not reinstall. In the intended regular Chrome profile, open a regular window and ensure Agent Browser is enabled at chrome://extensions/. Keep it enabled in only one profile and check again.",
                          next_action="check")
        output = blocker(browser, reply)
        if state == "extension_required" or output.get("update_required") is True:
            output["next_action"] = "prepare"
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

    def prepare(self, approved, update, scope):
        self.grants.revoke_scope(scope)
        if not approved:
            message = ("Ask the user before staging an update to the existing extension and its native messaging host. The user reloads the extension themselves."
                       if update else "Ask the user before staging the extension files and registering the local native messaging host. The user loads the extension themselves; setup does not open the requested page.")
            return result("consent_required", "chrome", message,
                          next_action="prepare")
        state = self.check("chrome")
        if not self.can_stage(state, update):
            return state
        profiles = self.profiles()
        grant = self.grants.issue(scope, (profiles, update))
        if not grant:
            return result("setup_error", "chrome", "Too many outstanding setup approvals. No changes were made.")
        multi = len(profiles) > 1
        return result("profile_required" if multi else "install_ready", "chrome",
                      "Ask which regular Chrome profile to use. Do not guess." if multi else "Use the only listed Chrome profile.",
                      profiles=self.choices(profiles), grant=grant, expires_in_seconds=GRANT_SECONDS, next_action="install",
                      **({"update_required": True} if update else {}))

    @staticmethod
    def can_stage(state, update):
        if update:
            return state["status"] == "extension_disconnected" and state.get("update_required") is True
        return state["status"] == "extension_required"

    def install(self, grant, profile, scope):
        missing = result("grant_required", "chrome", "This session has no valid grant. Obtain fresh user approval before preparing again.", next_action="prepare")
        entry = self.grants.get(grant, scope)
        if entry is None:
            return missing
        profiles, update = entry
        if profile is None and len(profiles) > 1:
            return result("profile_required", "chrome", "Ask which listed Chrome profile to use before installing.",
                          profiles=self.choices(profiles), next_action="install")
        profile = profile if profile is not None else next(iter(profiles))
        if profile not in profiles:
            return result("profile_invalid", "chrome", "Use one of the regular Chrome profiles returned by prepare.", next_action="install")
        # A valid install attempt consumes consent even if the subsequent preflight fails.
        self.grants.discard(grant)
        state = self.check("chrome")
        if not self.can_stage(state, update):
            return state
        if self.profiles() != profiles:
            return result("profiles_changed", "chrome", "Chrome profiles changed. Ask for fresh approval and profile selection.", next_action="prepare")
        backend = self.service.backends["chrome"]
        backend.install(profile)
        choice = self.choices({profile: profiles[profile]})[0]
        if update:
            self.staged_update = choice
        return self.pending(backend, choice, update=update)
