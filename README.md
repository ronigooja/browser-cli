# safari-cli

A dependency-free Bash tool for the user's real, logged-in macOS Safari through AppleScript.
The user logs in themselves; the tool stores no credentials or page markers.

## Setup

Requires macOS, Safari, and the system Bash/AppleScript tools.
Turn on Safari > Settings > Developer > **Allow JavaScript from Apple Events** once by hand
(requires Touch ID or a password). Leave it on; this tool never changes the setting
and never quits or restarts Safari. The first run may prompt macOS to let the calling
program control Safari; allow it.

Copy `safari-cli` to a directory on your PATH and make it executable.

## Commands

Syntax: `safari-cli <command> [options]`; `help` prints the reference, `version` the version.

| Command | Behavior |
| --- | --- |
| `status` | Probe `1+1` in an existing tab: print `on`, `off`, or `unknown` (no window/tab); never open a window. |
| `tabs [--json]` | List tabs as three TAB-separated fields: `<window>:<tab>`, title, URL. `--json` prints an array of `{window,tab,title,url}` objects. |
| `open <url>` | Focus a tab whose URL starts with the URL, otherwise open one; print `<window>:<tab><TAB>reused` or `<window>:<tab><TAB>created`. |
| `close [--tab T]` | Close the selected tab. |
| `info [--tab T]` | Print title, URL, readyState, and body-text/HTML character counts as `key: value` lines. |
| `text [--tab T] [--selector CSS] [--max-chars N]` | Print whitespace-collapsed `innerText`, excluding script/style content. |
| `links [--tab T] [--selector CSS] [--max-chars N]` | Print one `<text><TAB>absolute URL` per link. |
| `html [--tab T] [--selector CSS] [--max-chars N] [--raw]` | Print outerHTML; by default removes script/style/svg/noscript, comments, and inline styles. `--raw` returns unmodified HTML. |
| `snapshot [--tab T] [--selector CSS] [--max-chars N]` | List visible interactive elements as `<index><TAB><tag/role><TAB><label><TAB><selector>`; selector values can be passed to page actions. Password values are never shown. |
| `click [--tab T] <css>` | Click the matching element; print `clicked <tag> "<text>"`. |
| `fill [--tab T] <css> <text>` | Set a field and dispatch input/change events; print `filled <tag> (<N> chars)`. Native setters support React/Vue inputs. |
| `select [--tab T] <css> <value>` | Select an option by value or label; print `selected "<label>"`. |
| `press [--tab T] <key> [--selector CSS]` | Press Enter, Escape, Tab, arrows, Backspace, Space, or a single character; print `pressed <key>`. Enter in a form submits it. |
| `scroll [--tab T] [--selector CSS] (--to top\|bottom \| --by N)` | Scroll the page or selected element; print `scrollY: <n> of <max>`. |
| `submit [--tab T] <css>` | Submit the matching form; print `submitted form`. |
| `eval [--tab T] <js>` | Evaluate an expression. |
| `file [--tab T] <path.js>` | Evaluate an expression read from a UTF-8 file. |
| `wait [--tab T] <js>` | Poll until truthy, printing nothing on success; report the last evaluation error on timeout. |

Exit codes: 0 success (`status`: on), 1 failure (including JavaScript errors,
lookup failures, timeout, or status off/unknown), 2 usage error.

## Options and results

- `--tab T`: 1-based `<window>:<tab>` or a URL substring (first match across windows);
  default is the front window's current tab. Indexes may change; prefer URL substrings.
- `--selector CSS`: `text`, `links`, and `html` restrict output to the first matching element;
  `snapshot` filters the elements listed; `press` and `scroll` target the matching element.
  A missing match prints `safari-cli: no element matches: <css>` to stderr and exits 1.
- `--max-chars N`: `text`, `links`, `html`, and `snapshot` default to 20000 characters. `eval` and `file`
  truncate only when this option is explicitly supplied. Truncation reports the emitted and
  total character counts to stderr and succeeds (exit 0).
- `--raw`: `html` only; preserve the original markup.
- `--json`: `tabs` only; return a JSON array of objects with `window`, `tab`, `title`, and `url`.
- `--to top|bottom` and `--by N`: `scroll` only; scroll to an endpoint or by a number of pixels.
- `--background`: `open` only. Do not activate Safari, raise a window, or switch the visible tab.
  Existing match reports `reused`; otherwise add a tab in window 1 and report `created`,
  restoring the previous current tab. Close only tabs reported as `created` by your own `open`.
  Safari not running: launch it in the background. No windows: a new window is still created.
- `--timeout N`: positive integer seconds for a command, default 30; wait polls share one deadline.
- `--`: end option parsing.

Strings are printed exactly, including trailing newlines; other values print JSON plus a
newline, and `undefined` prints an empty line. Shell command substitution strips trailing
newlines, so redirect to a file for exact comparisons.
In `tabs` output, titles have embedded tabs and newlines replaced with spaces. Other
AppleScript failures print `safari-cli: AppleScript error <code>: <message>` to stderr
(`unknown` is used when no error code is available). A selector miss prints
`safari-cli: no element matches: <css>` to stderr.
Clicking a hidden element is an error (exit 1). All page actions run inside the page via
JavaScript, so dispatched events are synthetic (`isTrusted` is false). They do not activate
Safari, switch tabs, or require extra permissions, and work with `--background`. Some sites
(including strong anti-bot and some payment/CAPTCHA pages) ignore synthetic events: the command
reports what it dispatched, but the page may not react. Verify with `text`, `snapshot`, or
`wait` and tell the user if it did not react; do not retry blindly. Real OS-level input is not
supported.
Quotes, backslashes, and Unicode pass through a UTF-8 temporary file. Shell quoting still
applies. Wrap statements in an immediately invoked function; first/last-line comments work.
Evaluation is synchronous (promises are not awaited), in the page world and main frame only.

## Examples

```sh
safari-cli status
safari-cli open https://example.com/
safari-cli wait --timeout 20 --tab example.com 'document.querySelector("h1") !== null'
safari-cli eval --tab example.com '({url: location.href, title: document.title})'
safari-cli file --tab 1:2 ./inspect.js
```

## Security

Leaving the permission on lets any program allowed to control Safari run JavaScript in
logged-in pages. Review access in System Settings > Privacy & Security > Automation.
Run only trusted code; do not paste cookies or tokens into logs.
