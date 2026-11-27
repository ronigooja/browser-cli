"""Shared validation and output bounds for the extension-backed browser service."""

import re
from collections import Counter
from urllib.parse import parse_qsl, urlsplit, urlunsplit


BROWSERS = ("safari", "chrome")
MODES = ("public_background", "existing_tab", "session_tab")
POLICIES = ("exact", "allow_added_query")
TOKEN_PATTERN = r"[A-Za-z0-9_-]{32}"
QUERY_RETRY = ("query_added", "query_serialization_changed")


def result(status, browser=None, message=None, **fields):
    return {"status": status, **({"browser": browser} if browser else {}),
            **({"message": message} if message else {}), **fields}


def url_key(value):
    if (not isinstance(value, str) or len(value) > 2048 or not value.startswith(("http://", "https://"))
            or any(c.isspace() or ord(c) < 32 or ord(c) == 127 or c == "\\" for c in value)):
        raise ValueError("Use an absolute HTTP(S) URL without credentials or whitespace.")
    value.encode("utf-8")  # Reject unpaired surrogates in URLs supplied by either side.
    url = urlsplit(value)
    if not url.hostname or url.username is not None or url.password is not None:
        raise ValueError("Use an absolute HTTP(S) URL without credentials.")
    port = url.port
    return (url.scheme, url.hostname, port if port is not None else (443 if url.scheme == "https" else 80),
            url.path or "/", url.query, url.fragment)


def query_pairs(query):
    if re.search(r"%(?![0-9a-fA-F]{2})", query):
        raise ValueError("Malformed query encoding")
    return Counter(parse_qsl(query, keep_blank_values=True, errors="strict"))


def url_mismatch(url, expected, policy):
    try:
        actual = url_key(url)
    except ValueError:
        return "invalid_observed_url"
    if actual[:3] != expected[:3]:
        return "origin_changed"
    if actual[3] != expected[3]:
        return "path_changed"
    if actual[5] != expected[5]:
        return "fragment_changed"
    if actual[4] == expected[4]:
        return None
    try:
        requested, observed = query_pairs(expected[4]), query_pairs(actual[4])
    except ValueError:
        return "query_changed"
    requested_keys = {key for key, _ in requested}
    if any(count != observed[pair] for pair, count in requested.items()) or any(
            pair[0] in requested_keys and pair not in requested for pair in observed):
        return "requested_query_changed"
    if policy == "allow_added_query":
        return None
    return "query_added" if observed - requested else "query_serialization_changed"


def sanitized_url(url):
    try:
        url_key(url)
        parsed = urlsplit(url)
    except ValueError:
        return None
    return urlunsplit((parsed.scheme, parsed.netloc, parsed.path or "/",
                       "REDACTED" if parsed.query else "", "REDACTED" if parsed.fragment else ""))


def token_valid(value):
    return isinstance(value, str) and re.fullmatch(TOKEN_PATTERN, value) is not None


def clipped(value, units):
    return value.encode("utf-16-le", errors="replace")[:units * 2].decode("utf-16-le", errors="ignore")


def page_output(page, links):
    original = page.get("text")
    if not isinstance(original, str):
        raise ValueError("Invalid page text")
    if not original.strip():
        return None
    original_title = page.get("title", "")
    if not isinstance(original_title, str):
        raise ValueError("Invalid page title")
    text, title = clipped(original, 12000), clipped(original_title, 500)
    truncated = page.get("truncated") is True or text != original or title != original_title
    fields = {"text": text, "title": title, "final_url": page["final_url"]}
    if links:
        budget = 8000
        fields["links"] = []
        entries = page.get("links", [])
        if not isinstance(entries, list):
            raise ValueError("Invalid page links")
        for entry in entries[:256]:
            if not isinstance(entry, dict) or not isinstance(entry.get("text"), str):
                continue
            url = entry.get("url")
            try:
                url_key(url)
            except ValueError:
                continue
            label = clipped(entry["text"], 200)
            cost = (len(url.encode("utf-16-le")) + len(label.encode("utf-16-le"))) // 2
            if len(fields["links"]) == 25 or cost > budget:
                truncated = True
                break
            budget -= cost
            truncated = truncated or label != entry["text"]
            fields["links"].append({"text": label, "url": url})
        truncated = truncated or len(entries) > 256
    return dict(fields, truncated=truncated)
