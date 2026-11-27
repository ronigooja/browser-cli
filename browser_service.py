"""Tool routing, target authorization, and bounded reads over browser backends."""

from dataclasses import dataclass
import secrets
import time

from browser_policy import (BROWSERS, MODES, POLICIES, QUERY_RETRY, clipped, page_output,
                            result, sanitized_url, token_valid, url_key, url_mismatch)


REQUEST_TIMEOUT = 5
CONTENT_TIMEOUT = 8
PASSIVE_TIMEOUT = 600
TAB_LIMIT = 128
BLOCKERS = {
    "extension_required": "Ask whether the user wants to set up the selected browser extension. Never install automatically.",
    "extension_pending": "Finish the browser extension setup with setup_extension; do not reinstall.",
    "extension_disconnected": "Check the extension in the intended browser/profile with setup_extension. Do not reinstall or fall back to a foreground open.",
    "extension_ambiguous": "More than one browser extension context is connected. Ask the user to identify the intended context; do not read or open a page.",
    "permission_required": "Ask the user to enable website access for all sites once in the browser's extension settings, then check the connection. Do not request a per-site grant.",
    "background_unavailable": "The browser has no verified way to open this page in the background. No foreground fallback was attempted.",
    "profile_required": "Select the intended named Safari profile in the Agent Browser setup popup and configure the displayed profile UUID in the MCP process environment.",
    "private_access_enabled_or_unverified": "Disable the Safari extension's access to Private Browsing and check again. No page was accessed.",
    "busy": "Another request is already using this browser context. No new operation was sent.",
    "read_unavailable": "The browser could not verify or access the selected document. No page content was returned.",
    "open_unconfirmed": "The browser could not confirm whether a tab opened. Inspect the browser before trying again; never fall back to a foreground open.",
    "response_too_large": "The browser response exceeded the transport limit. No page content was returned.",
    "timeout": "The browser operation timed out. Inspect the browser before retrying; a tab might have opened.",
    "browser_error": "Could not verify a valid response from the selected browser.",
    "backend_unavailable": "The production extension transport for the selected browser is unavailable. No browser operation was attempted.",
}
PAGE_MESSAGES = {
    "login_required": "Complete sign-in in the browser yourself. Then open the intended page and use session_tab.",
    "verification_required": "Complete human verification in the browser yourself, then retry the same read.",
    "passive_challenge": "A passive challenge is still present. Wait for it in the browser and retry later.",
    "page_loading": "The page is still loading. Retry after it finishes.",
    "no_content": "The page has no visible text. This does not establish a login requirement.",
    "no_match": "No element matches the requested selector.",
    "invalid_selector": "The CSS selector is invalid.",
    "invalid_wait_selector": "The wait_for_selector CSS selector is invalid.",
    "content_not_ready": "The requested visible content did not appear before the wait ended. This does not establish a login requirement.",
}


def blocker(browser, reply):
    status = reply.get("status")
    if status == "ambiguous":
        status = "extension_ambiguous"
    if not isinstance(status, str) or status not in BLOCKERS:
        status = "browser_error"
    fields = {}
    if status == "backend_unavailable":
        fields["reason"] = ("safari_production_host_unavailable" if browser == "safari"
                            else "chrome_transport_unavailable")
    if browser == "chrome" and status == "extension_disconnected" and reply.get("update_required") is True:
        return result(status, browser, "The Chrome extension needs an update. Use setup_extension to check and obtain explicit approval before staging the update, then have the user reload the extension.", update_required=True)
    return result(status, browser, BLOCKERS[status], **fields)


def mismatch(browser, reason, observed=None, status="target_changed"):
    fields = {"mismatch_reason": reason}
    if safe_url := sanitized_url(observed):
        fields["observed_url"] = safe_url
    if reason in QUERY_RETRY:
        fields["retry_url_policy"] = "allow_added_query"
    return result(status, browser, "The target tab or document URL did not match. No page content was returned.", **fields)


class Capabilities:
    def __init__(self, clock, ttl, limit):
        self.clock, self.ttl, self.limit = clock, ttl, limit
        self.entries = {}

    def prune(self):
        now = self.clock()
        self.entries = {token: item for token, item in self.entries.items() if item[1] > now}

    def issue(self, scope, value):
        self.prune()
        if len(self.entries) >= self.limit:
            return None
        token = secrets.token_urlsafe(24)
        self.entries[token] = (scope, self.clock() + self.ttl, value)
        return token

    def get(self, token, scope):
        self.prune()
        entry = self.entries.get(token)
        return entry[2] if entry and entry[0] == scope else None

    def discard(self, token):
        self.entries.pop(token, None)

    def revoke_scope(self, scope):
        self.entries = {token: item for token, item in self.entries.items() if item[0] != scope}


@dataclass(frozen=True)
class Tab:
    ref: str
    url: str
    title: str

    @classmethod
    def from_reply(cls, tab):
        if (not isinstance(tab, dict) or not isinstance(tab.get("ref"), str) or not 0 < len(tab["ref"]) <= 1024
                or not isinstance(tab.get("url"), str) or not isinstance(tab.get("title"), str)):
            raise ValueError("Invalid tab metadata")
        return cls(tab["ref"], tab["url"], clipped(tab["title"], 500))

    def candidate(self):
        return {"title": self.title, "url": sanitized_url(self.url)}


@dataclass(frozen=True)
class Owned:
    browser: str
    request_url: str
    ref: str
    marker: str


class BrowserService:
    def __init__(self, backends, clock=time.monotonic, sleep=time.sleep):
        self.backends, self.clock, self.sleep = backends, clock, sleep
        self.candidates = Capabilities(clock, 300, 256)
        self.owned = Capabilities(clock, 3600, 256)
        self.resumes = Capabilities(clock, 3600, 256)

    def request(self, browser, operation, payload=None, timeout=REQUEST_TIMEOUT):
        backend = self.backends.get(browser)
        if backend is None:
            return {"status": "backend_unavailable"}
        try:
            reply = backend.request(operation, payload or {}, timeout)
        except TimeoutError:
            return {"status": "timeout"}
        except (OSError, EOFError, ValueError, TypeError, KeyError, AttributeError):
            return {"status": "browser_error"}
        if not isinstance(reply, dict) or not isinstance(reply.get("status"), str):
            return {"status": "browser_error"}
        normalized = {"missing": "extension_required", "pending": "extension_pending", "no_regular_window": "background_unavailable",
                      "window_snapshot_unavailable": "background_unavailable", "site_access_required": "permission_required",
                      "disconnected": "extension_disconnected", "ambiguous": "extension_ambiguous"}.get(reply["status"])
        if normalized:
            return dict(reply, status=normalized)
        return reply

    def find(self, browser, url):
        reply = self.request(browser, "find", {"url": url})
        if reply["status"] == "tab_ambiguous":
            return [], result("tab_ambiguous", browser, "The browser could not identify a unique tab. No page content was returned.")
        if reply["status"] != "ok":
            return [], blocker(browser, reply)
        entries = reply.get("tabs")
        if not isinstance(entries, list):
            raise ValueError("Invalid tab list")
        if len(entries) > TAB_LIMIT or reply.get("truncated") is True:
            return [], result("tab_ambiguous", browser, "Too many possible tabs; no tab was selected.")
        tabs = [Tab.from_reply(tab) for tab in entries]
        if len({tab.ref for tab in tabs}) != len(tabs):
            raise ValueError("Non-unique tab refs")
        return tabs, None

    def cleanup(self, args, scope):
        if not isinstance(args, dict) or set(args) != {"cleanup_token"} or not token_valid(args["cleanup_token"]):
            raise ValueError("Provide the cleanup_token returned by read_page.")
        token = args["cleanup_token"]
        entry = self.owned.get(token, scope)
        if entry is None:
            return result("cleanup_unavailable", message="The token is unknown, expired, or from another session. Leave the tab open.")
        self.owned.discard(token)
        reply = self.request(entry.browser, "close", {"ref": entry.ref, "ownership": entry.marker})
        messages = {
            "closed": None,
            "cleanup_instance_changed": "The original browser instance could not be verified; no tab was closed.",
            "cleanup_tab_not_found": "The original tab was not found in that instance; no tab was closed.",
            "cleanup_unconfirmed": "Could not confirm tab closure. Do not retry with a URL, index, or another identifier.",
        }
        status = reply["status"] if reply["status"] in messages else "cleanup_unconfirmed"
        return result(status, entry.browser, messages[status])

    def choose(self, browser, matches, url, policy, scope, authorize):
        choices = [tab.candidate() for tab in matches[:10]]
        if authorize:
            for tab, choice in zip(matches, choices):
                token = self.candidates.issue(scope, (browser, url, policy, tab.ref, tab.url))
                if token:
                    choice["candidate_token"] = token
            if not any("candidate_token" in choice for choice in choices):
                return result("tab_confirmation_unavailable", browser, "Too many outstanding confirmations. No page content was returned.")
            if len(choices) == 1:
                return result("tab_confirmation_required", browser,
                              "Show the title and URL to the user. Use the candidate token only after they confirm this tab and browser profile.",
                              candidate=choices[0])
        return result("tab_ambiguous", browser, "More than one tab matches. Ask the user which tab is intended; do not guess.",
                      candidate_count=len(matches), candidates=choices)

    def read(self, args, scope):
        allowed = {"browser", "url", "mode", "url_policy", "candidate_token", "resume_token",
                   "selector", "wait_for_selector", "include_links"}
        if not isinstance(args, dict) or args.keys() - allowed:
            raise ValueError("Invalid read_page arguments.")
        url = args.get("url")
        try:
            expected = url_key(url)
        except ValueError:
            raise ValueError("Use a valid absolute HTTP(S) URL without credentials or whitespace.") from None
        browser, mode, policy = args.get("browser"), args.get("mode", MODES[0]), args.get("url_policy", POLICIES[0])
        if mode not in MODES or policy not in POLICIES or type(args.get("include_links", False)) is not bool:
            raise ValueError("Invalid mode, url_policy, or include_links.")
        for name in ("selector", "wait_for_selector"):
            if name in args:
                value = args[name]
                if not isinstance(value, str) or not value.strip() or len(value) > 1000 or "\x00" in value:
                    raise ValueError(f"{name} must be a nonempty CSS selector of at most 1000 characters.")
        for field, required_mode in (("candidate_token", "session_tab"), ("resume_token", "public_background")):
            if field in args and (mode != required_mode or browser not in BROWSERS or not token_valid(args[field])):
                raise ValueError(f"Use {field} only with the original browser and {required_mode} mode.")
        if "browser" not in args:
            return result("browser_required", message=("Ask which browser holds the required login." if mode == "session_tab"
                                                        else "Ask only which browser to use: Safari or Chrome."), choices=list(BROWSERS))
        if browser not in BROWSERS:
            raise ValueError("Choose safari or chrome.")
        candidate_token, resume_token = args.get("candidate_token"), args.get("resume_token")
        candidate = self.candidates.get(candidate_token, scope) if candidate_token else None
        if candidate_token and (candidate is None or candidate[:3] != (browser, url, policy)):
            return mismatch(browser, "candidate_unavailable")
        resume = self.resumes.get(resume_token, scope) if resume_token else None
        owned = self.owned.get(resume[0], scope) if resume else None
        if resume_token and (owned is None or (owned.browser, owned.request_url, resume[1]) != (browser, url, policy)):
            return mismatch(browser, "resume_unavailable")

        fields = {}
        try:
            response = self.read_selected(args, scope, browser, mode, url, expected, policy, candidate, owned, fields)
        except (ValueError, TypeError, KeyError, AttributeError):
            response = blocker(browser, {"status": "browser_error"})
        # A retry token never grants permission to close a tab, and resolves through live ownership.
        retry_policy = response.get("retry_url_policy")
        if not retry_policy and response["status"] in ("verification_required", "passive_challenge", "page_loading", "no_content", "content_not_ready"):
            retry_policy = policy
        cleanup = fields.get("cleanup_token") or (resume[0] if owned else None)
        if cleanup and retry_policy and retry_policy in POLICIES:
            retry = resume_token if resume and resume[1] == retry_policy else self.resumes.issue(scope, (cleanup, retry_policy))
            if retry:
                fields["resume_token"] = retry
        return dict(response, **fields)

    def read_selected(self, args, scope, browser, mode, url, expected, policy, candidate, owned, fields):
        state = self.request(browser, "status")
        if (browser == "chrome" and mode == "public_background" and owned is None
                and state["status"] == "extension_disconnected" and state.get("update_required") is not True):
            # The bridge may wake a verified running, windowless Chrome. Never pass a URL
            # in this step or try to recover an existing tab's lost connection identity.
            state = self.request(browser, "wake")
        if state["status"] != "ok":
            return blocker(browser, state)
        tabs, failure = self.find(browser, url)
        if failure:
            return failure
        created = owned is not None
        if candidate or owned:
            pinned_ref = candidate[3] if candidate else owned.ref
            tab = next((tab for tab in tabs if tab.ref == pinned_ref), None)
            if tab is None:
                return mismatch(browser, "tab_not_found")
            if reason := url_mismatch(tab.url, expected, policy):
                return mismatch(browser, reason, tab.url)
            if candidate and tab.url != candidate[4]:
                return mismatch(browser, "candidate_url_changed", tab.url)
        else:
            matches = [tab for tab in tabs if not url_mismatch(tab.url, expected, policy)]
            if matches and (mode == "session_tab" or len(matches) > 1):
                return self.choose(browser, matches, url, policy, scope, mode == "session_tab")
            if matches:
                tab = matches[0]
            else:
                alternate = [tab for tab in tabs if url_mismatch(tab.url, expected, policy) in QUERY_RETRY]
                if len(alternate) == 1:
                    return mismatch(browser, url_mismatch(alternate[0].url, expected, policy), alternate[0].url)
                if len(alternate) > 1:
                    return self.choose(browser, alternate, url, policy, scope, False)
                if mode != "public_background":
                    return result("open_tab_required", browser, "Ask the user to open the exact URL in the intended browser/profile, then try again.")
                opened = self.request(browser, "open", {"url": url})
                if opened["status"] == "tab_ambiguous":
                    entries = opened.get("tabs")
                    if isinstance(entries, list) and 1 < len(entries) <= TAB_LIMIT:
                        return self.choose(browser, [Tab.from_reply(entry) for entry in entries], url, policy, scope, False)
                    return result("tab_ambiguous", browser, "More than one tab matches. No tab was selected or opened.")
                if opened["status"] != "ok":
                    return blocker(browser, opened)
                tab = Tab.from_reply(opened.get("tab"))
                created = opened.get("created")
                if type(created) is not bool:
                    raise ValueError("Invalid open response")
                if created and any(existing.ref == tab.ref for existing in tabs):
                    return mismatch(browser, "created_tab_already_present")
                fields["tab_disposition"] = "created" if created else "reused"
                if created:
                    marker = opened.get("ownership")
                    if isinstance(marker, str) and 0 < len(marker) <= 4096:
                        token = self.owned.issue(scope, Owned(browser, url, tab.ref, marker))
                        if token:
                            fields["cleanup_token"] = token
                    if "cleanup_token" not in fields:
                        fields["cleanup_note"] = "The created tab cannot be safely identified for automated cleanup; leave it open."
                reason = url_mismatch(tab.url, expected, policy)
                if reason:
                    return mismatch(browser, reason, tab.url, "target_changed" if created else "reused_tab_mismatch")
        return self.read_tab(args, browser, url, expected, policy, tab, created, candidate is not None)

    def read_tab(self, args, browser, url, expected, policy, tab, created, confirmed):
        payload = {"ref": tab.ref, "expected_url": tab.url}
        payload.update({key: args[key] for key in ("selector", "wait_for_selector", "include_links") if key in args})
        content_deadline = self.clock() + CONTENT_TIMEOUT
        passive_deadline = None
        deadline = content_deadline
        while True:
            page = self.request(browser, "read", payload, min(REQUEST_TIMEOUT, max(0.001, deadline - self.clock())))
            status = page["status"]
            observed = page.get("final_url", page.get("document_url"))
            if observed is not None and observed != payload["expected_url"]:
                reason = url_mismatch(observed, expected, policy)
                if reason and not (status == "target_changed" and created and observed in ("", "about:blank")):
                    return mismatch(browser, reason, observed)
                if status != "target_changed":
                    return mismatch(browser, "document_url_changed", observed)
            if status == "target_changed":
                if confirmed or self.clock() >= content_deadline:
                    return mismatch(browser, "document_url_changed", observed)
                # Renew the exact document guard only on the same transport ref. A newly
                # created tab can temporarily have a blank document while loading.
                tabs, failure = self.find(browser, url)
                if failure:
                    return failure
                current = next((item for item in tabs if item.ref == tab.ref), None)
                if current is None:
                    return mismatch(browser, "tab_not_found")
                if reason := url_mismatch(current.url, expected, policy):
                    return mismatch(browser, reason, current.url)
                if current.url == payload["expected_url"] and not (created and observed in ("", "about:blank")):
                    return mismatch(browser, "document_url_changed", observed)
                payload["expected_url"] = current.url
                deadline, interval = content_deadline, 0.25
            elif status == "ok":
                if page.get("final_url") != payload["expected_url"]:
                    return mismatch(browser, "document_url_changed", observed)
                fields = page_output(page, args.get("include_links", False))
                if fields is None:
                    status = "no_content"
                    if not created:
                        return result(status, browser, PAGE_MESSAGES[status])
                    deadline, interval = content_deadline, 0.25
                else:
                    return result("ok", browser, **fields)
            elif status == "passive_challenge":
                if passive_deadline is None:
                    passive_deadline = self.clock() + PASSIVE_TIMEOUT
                deadline, interval = passive_deadline, 1
            elif status == "page_loading" or (status == "no_content" and created) or (
                    status == "content_not_ready" and "wait_for_selector" in args):
                deadline, interval = content_deadline, 0.25
            elif status in PAGE_MESSAGES:
                return result(status, browser, PAGE_MESSAGES[status])
            else:
                return blocker(browser, page)
            remaining = deadline - self.clock()
            if remaining <= 0:
                if status == "target_changed":
                    return mismatch(browser, "document_url_changed", observed)
                return result(status, browser, PAGE_MESSAGES[status])
            self.sleep(min(interval, remaining))
            if self.clock() >= deadline:
                if status == "target_changed":
                    return mismatch(browser, "document_url_changed", observed)
                return result(status, browser, PAGE_MESSAGES[status])
