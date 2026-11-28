"""Session-scoped controls for the authorized browser profile."""

from dataclasses import dataclass

from browser_policy import BROWSERS, clipped, result, token_valid, url_key, url_mismatch
from browser_service import (PAGE_MESSAGES, REQUEST_TIMEOUT, Capabilities, Owned, Tab,
                             blocker)


PAGE_FIELDS = {
    "info": (), "html": ("selector",), "snapshot": ("selector", "offset"),
    "extract": ("selector", "offset"), "wait": ("selector", "condition", "text"),
    "click": ("selector", "element_ref"), "scroll": ("selector", "element_ref", "direction", "pixels"),
    "fill": ("selector", "element_ref", "text"), "select": ("selector", "element_ref", "value"),
    "press": ("selector", "element_ref", "key"), "submit": ("selector", "element_ref"),
}
PAGE_WRITES = frozenset(("click", "scroll", "fill", "select", "press", "submit"))
TAB_ACTIONS = ("list", "confirm", "open_background", "open_foreground", "navigate", "activate", "close")
PAGE_ERRORS = {**PAGE_MESSAGES,
    "stale_element": "The element reference is no longer valid. Take a new snapshot before trying again.",
    "action_unavailable": "The requested action is not available for the selected element or page.",
    "action_unconfirmed": "The browser could not confirm the action. Inspect the intended tab before retrying.",
    "target_changed": "The confirmed tab or document changed. Select and confirm the intended tab again.",
}


@dataclass(frozen=True)
class Target:
    browser: str
    tab: Tab


class BrowserOperations:
    def __init__(self, service):
        self.service = service
        self.candidates = Capabilities(service.clock, 300, 256)
        self.targets = Capabilities(service.clock, 1800, 256)

    def fail(self, browser, status, *, write=False):
        if write and status in ("timeout", "browser_error", "read_unavailable", "response_too_large", "open_unconfirmed"):
            status = "action_unconfirmed"
        if status in PAGE_ERRORS:
            return result(status, browser, PAGE_ERRORS[status])
        return blocker(browser, {"status": status})

    @staticmethod
    def validate(args, common, fields, required):
        if args.keys() - set(common) - set(fields) or not set(required) <= args.keys():
            raise ValueError("Invalid arguments for this browser operation.")
        for key in ("tab_ref", "candidate_token"):
            if key in args and not token_valid(args[key]):
                raise ValueError(f"Use an issued {key} from this session.")
        for key, limit in (("selector", 1000), ("element_ref", 256), ("text", 4000),
                           ("value", 1000), ("key", 32)):
            if key in args:
                value = args[key]
                if (not isinstance(value, str) or len(value) > limit or "\x00" in value
                        or key not in ("text", "value") and not value.strip()):
                    raise ValueError(f"Invalid {key}.")
        for key, low, high in (("offset", 0, 10000), ("pixels", -10000, 10000), ("timeout_ms", 0, 30000)):
            if key in args and (type(args[key]) is not int or not low <= args[key] <= high):
                raise ValueError(f"Invalid {key}.")
        if "approved" in args and type(args["approved"]) is not bool:
            raise ValueError("Invalid approved.")
        if "url" in args:
            url_key(args["url"])
        if "selector" in args and "element_ref" in args:
            raise ValueError("Choose either selector or element_ref.")

    def issue(self, target, scope, candidate=False):
        field, store = ("candidate_token", self.candidates) if candidate else ("tab_ref", self.targets)
        token = store.issue(scope, target)
        return {field: token} if token else {}

    def tabs(self, args, scope):
        if not isinstance(args, dict) or args.get("action") not in TAB_ACTIONS or args.get("browser") not in BROWSERS:
            raise ValueError("Choose a tab action and Safari or Chrome.")
        browser, action = args["browser"], args["action"]
        params = {
            "list": (), "confirm": ("candidate_token", "approved"), "open_background": ("url",),
            "open_foreground": ("url", "approved"), "navigate": ("tab_ref", "url"),
            "activate": ("tab_ref", "approved"), "close": ("tab_ref", "approved"),
        }[action]
        self.validate(args, ("action", "browser"), params, params)
        if "approved" in params and args["approved"] is not True:
            return result("approval_required", browser, "Ask the user to confirm this specific operation first.")
        request = self.service.request
        if action in ("list", "confirm"):
            target = None
            if action == "confirm":
                target = self.candidates.get(args["candidate_token"], scope)
                if target is None or target.browser != browser:
                    return self.fail(browser, "target_changed")
            reply = request(browser, "list" if action == "list" else "find",
                            None if target is None else {"url": target.tab.url})
            if reply["status"] != "ok":
                return blocker(browser, reply)
            rows = reply.get("tabs")
            if not isinstance(rows, list) or len(rows) > (32 if target is None else 128):
                return self.fail(browser, "browser_error")
            try:
                entries = [Tab.from_reply(row) for row in rows]
                for tab in entries:
                    url_key(tab.url)
            except ValueError:
                return self.fail(browser, "browser_error")
            if len({tab.ref for tab in entries}) != len(entries):
                return self.fail(browser, "browser_error")
            if target is not None:
                if reply.get("truncated") is True or not any(tab.ref == target.tab.ref and tab.url == target.tab.url for tab in entries):
                    return self.fail(browser, "target_changed")
                self.candidates.discard(args["candidate_token"])
                capability = self.issue(target, scope)
                if not capability:
                    return result("tab_confirmation_unavailable", browser)
                return result("ok", browser, **target.tab.candidate(), **capability)
            fields = []
            for tab, row in zip(entries, rows):
                item = tab.candidate()
                if type(row.get("active")) is bool:
                    item["active"] = row["active"]
                if type(row.get("window_id")) in (str, int):
                    item["window_id"] = clipped(str(row["window_id"]), 80)
                fields.append(dict(item, **self.issue(Target(browser, tab), scope, candidate=True)))
            return result("ok", browser, tabs=fields, truncated=reply.get("truncated") is True)
        if action in ("open_background", "open_foreground"):
            url = args["url"]
            state = request(browser, "status")
            if (action == "open_background" and browser == "chrome" and state["status"] == "extension_disconnected"
                    and state.get("update_required") is not True):
                state = request(browser, "wake")
            if state["status"] != "ok":
                return blocker(browser, state)
            reply = request(browser, "open" if action == "open_background" else "front_open", {"url": url})
            if reply["status"] == "tab_ambiguous":
                return result("tab_ambiguous", browser, "List the tabs and ask which one is intended.")
            if reply["status"] != "ok":
                return self.fail(browser, reply["status"], write=True)
            try:
                tab = Tab.from_reply(reply.get("tab"))
            except ValueError:
                return self.fail(browser, "action_unconfirmed")
            created = reply.get("created")
            if type(created) is not bool:
                return self.fail(browser, "action_unconfirmed")
            fields = {"tab_disposition": "created" if created else "reused", **tab.candidate()}
            marker = reply.get("ownership")
            if created and action == "open_background" and isinstance(marker, str) and 0 < len(marker) <= 4096:
                token = self.service.owned.issue(scope, Owned(browser, url, tab.ref, marker))
                if token:
                    fields["cleanup_token"] = token
            if url_mismatch(tab.url, url_key(url), "exact"):
                return result("target_changed", browser, PAGE_ERRORS["target_changed"], **fields)
            fields.update(self.issue(Target(browser, tab), scope, candidate=not created))
            return result("ok", browser, **fields)
        token = args["tab_ref"]
        target = self.targets.get(token, scope)
        if target is None or target.browser != browser:
            return self.fail(browser, "target_changed")
        reply = request(browser, "tab", {"ref": target.tab.ref, "expected_url": target.tab.url, "kind": action,
                                         **({"url": args["url"]} if action == "navigate" else {})})
        if action in ("navigate", "close"):
            self.targets.discard(token)
        if reply["status"] != "ok":
            return self.fail(browser, reply["status"], write=True)
        if action == "navigate":
            # Explicit navigation never carries authorization across an unexpected redirect.
            try:
                tab = Tab.from_reply(reply.get("tab"))
            except ValueError:
                return result("ok", browser, "Navigation requested. List tabs again to select its destination.")
            if (tab.ref != target.tab.ref or url_mismatch(tab.url, url_key(args["url"]), "exact")
                    or url_key(tab.url)[:3] != url_key(target.tab.url)[:3]):
                return result("ok", browser, "Navigation requested. List tabs again to confirm its destination.")
            return result("ok", browser, **tab.candidate(), **self.issue(Target(browser, tab), scope))
        return result("ok", browser)

    def page(self, args, scope):
        if (not isinstance(args, dict) or not isinstance(args.get("action"), str)
                or args["action"] not in PAGE_FIELDS or args.get("browser") not in BROWSERS):
            raise ValueError("Choose a page action and Safari or Chrome.")
        browser, action = args["browser"], args["action"]
        extra = ("timeout_ms",) if action == "wait" else ("approved",) if action == "submit" else ()
        required = ("tab_ref",) + {"wait": ("condition",), "fill": ("text",), "select": ("value",),
                                    "press": ("key",), "submit": ("approved",)}.get(action, ())
        self.validate(args, ("action", "browser", "tab_ref"), PAGE_FIELDS[action] + extra, required)
        if action in ("click", "fill", "select", "submit") and not (args.get("selector") or args.get("element_ref")):
            raise ValueError("Choose a selector or a snapshot element_ref.")
        if action == "submit" and args["approved"] is not True:
            return result("approval_required", browser, "Ask before submitting the specified form.")
        if action == "scroll" and (args.get("direction", "by") not in ("by", "top", "bottom")
                or (args.get("direction", "by") == "by") != ("pixels" in args)):
            raise ValueError("Scroll by pixels or to top or bottom.")
        if action == "wait" and (args["condition"] not in ("visible", "hidden", "text")
                or (args["condition"] == "text" and not args.get("text"))
                or (args["condition"] != "text" and (not args.get("selector") or "text" in args))):
            raise ValueError("Wait for a selector to be visible or hidden, or for literal text.")
        target = self.targets.get(args["tab_ref"], scope)
        if target is None or target.browser != browser:
            return self.fail(browser, "target_changed")
        payload = {"ref": target.tab.ref, "expected_url": target.tab.url, "kind": action}
        payload.update({"elementRef" if key == "element_ref" else key: args[key]
                        for key in PAGE_FIELDS[action] if key in args})
        if action == "scroll":
            payload.setdefault("direction", "by")
        deadline = self.service.clock() + args.get("timeout_ms", 8000) / 1000
        timeout = min(REQUEST_TIMEOUT, args["timeout_ms"] / 1000) if args.get("timeout_ms", 0) else REQUEST_TIMEOUT
        while True:
            reply = self.service.request(browser, "page", payload, timeout)
            status = reply["status"]
            if status == "ok":
                if reply.get("final_url") != target.tab.url:
                    return self.fail(browser, "action_unconfirmed" if action in PAGE_WRITES else "target_changed")
                fields = ("title", "ready_state", "challenge", "text", "links", "html", "elements", "truncated",
                          "next_offset", "matched", "performed", "action")
                return result("ok", browser, final_url=target.tab.url,
                              **{key: reply[key] for key in fields if key in reply})
            remaining = deadline - self.service.clock()
            if action != "wait" or status not in ("content_not_ready", "page_loading") or remaining <= 0:
                return self.fail(browser, status, write=action in PAGE_WRITES)
            self.service.sleep(min(0.25, remaining))
            remaining = deadline - self.service.clock()
            if remaining <= 0:
                return self.fail(browser, status)
            timeout = min(REQUEST_TIMEOUT, remaining)
