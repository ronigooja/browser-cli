# browser-cli

A dependency-free Bash tool for the user's real, logged-in macOS Safari or Google Chrome through AppleScript.
The user logs in themselves; the tool stores no credentials or page markers.

## Setup

Requires macOS and the system Bash/AppleScript tools. Turn on the browser’s **Allow JavaScript from Apple Events** setting once by hand (requires Touch ID or a password): Safari > Settings > Developer, or Chrome > View > Developer. Leave it on; this tool never changes the setting and never quits or restarts the browser. The first run may prompt macOS to let the calling
program control the selected browser; allow it.

Copy `browser-cli` to a directory on your PATH and make it executable.

## Commands

Syntax: `browser-cli --browser safari|chrome <command> [options]`; `--browser` is required for every browser command (it may appear anywhere in the arguments). Missing it prints `browser-cli: --browser safari|chrome is required` and exits 2. `help` and `version` do not require it.

| Command | Behavior |
| --- | --- |
| `--browser safari|chrome status` | Probe `1+1` in an existing tab: print `on`, `off`, or `unknown` (no window/tab); never open a window. |
| `--browser safari|chrome tabs [--json]` | List tabs as three TAB-separated fields: `<window>:<tab>`, title, URL. `--json` prints an array of `{window,tab,title,url}` objects. |
| `--browser safari|chrome open <url>` | Focus a tab whose URL starts with the URL, otherwise open one; print `<window>:<tab><TAB>reused` or `<window>:<tab><TAB>created`. |
| `--browser safari|chrome close [--tab T]` | Close the selected tab. |
| `--browser safari|chrome info [--tab T]` | Print title, URL, readyState, body-text/HTML character counts, and final `challenge: none\|cloudflare-passive\|needs-human` line. |
| `--browser safari|chrome wait-challenge [--tab T] [--timeout N]` | Wait until no challenge page is present; print `challenge: cleared`. Default timeout is 600 seconds. |
| `--browser safari|chrome text [--tab T] [--selector CSS] [--max-chars N]` | Print whitespace-collapsed `innerText`, excluding script/style content. |
| `--browser safari|chrome links [--tab T] [--selector CSS] [--max-chars N]` | Print one `<text><TAB>absolute URL` per link. |
| `--browser safari|chrome html [--tab T] [--selector CSS] [--max-chars N] [--raw]` | Print outerHTML; by default removes script/style/svg/noscript, comments, and inline styles. `--raw` returns unmodified HTML. |
| `--browser safari|chrome snapshot [--tab T] [--selector CSS] [--max-chars N]` | List visible interactive elements as `<index><TAB><tag/role><TAB><label><TAB><selector>`; selector values can be passed to page actions. Password values are never shown. |
| `--browser safari|chrome click [--tab T] <css>` | Click the matching element; print `clicked <tag> "<text>"`. |
| `--browser safari|chrome fill [--tab T] <css> <text>` | Set a field and dispatch input/change events; print `filled <tag> (<N> chars)`. Native setters support React/Vue inputs. |
| `--browser safari|chrome select [--tab T] <css> <value>` | Select an option by value or label; print `selected "<label>"`. |
| `--browser safari|chrome press [--tab T] <key> [--selector CSS]` | Press Enter, Escape, Tab, arrows, Backspace, Space, or a single character; print `pressed <key>`. Enter in a form submits it. |
| `--browser safari|chrome scroll [--tab T] [--selector CSS] (--to top\|bottom \| --by N)` | Scroll the page or selected element; print `scrollY: <n> of <max>`. |
| `--browser safari|chrome submit [--tab T] <css>` | Submit the matching form; print `submitted form`. |
| `--browser safari|chrome eval [--tab T] <js>` | Evaluate an expression. |
| `--browser safari|chrome file [--tab T] <path.js>` | Evaluate an expression read from a UTF-8 file. |
| `--browser safari|chrome wait [--tab T] <js>` | Poll until truthy, printing nothing on success; report the last evaluation error on timeout. |
| `--browser safari|chrome screenshot [--tab T] [--selector CSS] [--out PATH]` | Save a PNG of the visible viewport or first matching element, and print its absolute path. |

Exit codes: 0 success (`status`: on), 1 failure (including JavaScript errors,
lookup failures, timeout, or status off/unknown), 2 usage error.

## Browser differences

| Feature | Safari | Chrome |
| --- | --- | --- |
| `screenshot` and `--trusted` | Supported | Not supported yet (exit 1: `browser-cli: <cmd> is not supported for chrome yet`) |
| Screenshot window capture | Safari must be the active app; Stage Manager can cause a thumbnail capture. | Not applicable |

## Options and results

- `--browser safari|chrome`: required for browser commands; chooses the user's browser.
- `--tab T`: 1-based `<window>:<tab>` or a URL substring (first match across windows);
  default is the front window's current tab. Indexes may change; prefer URL substrings.
- `--selector CSS`: `text`, `links`, and `html` restrict output to the first matching element;
  `snapshot` filters the elements listed; `press` and `scroll` target the matching element.
  A missing match prints `browser-cli: no element matches: <css>` to stderr and exits 1.
- `--max-chars N`: `text`, `links`, `html`, and `snapshot` default to 20000 characters. `eval` and `file`
  truncate only when this option is explicitly supplied. Truncation reports the emitted and
  total character counts to stderr and succeeds (exit 0).
- `--raw`: `html` only; preserve the original markup.
- `--json`: `tabs` only; return a JSON array of objects with `window`, `tab`, `title`, and `url`.
- `--to top|bottom` and `--by N`: `scroll` only; scroll to an endpoint or by a number of pixels.
- `--background`: `open` only. Do not activate the browser, raise a window, or switch the visible tab.
  Existing match reports `reused`; otherwise add a tab in window 1 and report `created`,
  restoring the previous current tab. Close only tabs reported as `created` by your own `open`.
  Browser not running: launch it in the background. No windows: a new window is still created.
- `--timeout N`: positive integer seconds for a command, default 30; wait polls share one deadline.
  `wait-challenge` defaults to 600 seconds.
- `--out PATH`: `screenshot` only; save to this path. By default, save as `browser-cli-<epoch>.png` in the temp directory.
- `--`: end option parsing.

Strings are printed exactly, including trailing newlines; other values print JSON plus a
newline, and `undefined` prints an empty line. Shell command substitution strips trailing
newlines, so redirect to a file for exact comparisons.
In `tabs` output, titles have embedded tabs and newlines replaced with spaces. Other
AppleScript failures print `browser-cli: AppleScript error <code>: <message>` to stderr
(`unknown` is used when no error code is available). A selector miss prints
`browser-cli: no element matches: <css>` to stderr.
Clicking a hidden element is an error (exit 1). Default page actions run inside the page via
JavaScript, so dispatched events are synthetic (`isTrusted` is false). They do not activate
the browser, switch tabs, or require extra permissions, and work with `--background`. Some sites
ignore synthetic events; verify with `text`, `snapshot`, or `wait`. If needed, `--trusted` on
`click`, `fill`, or `press` sends real OS-level input (`isTrusted=true`), requires Accessibility
permission, activates Safari, selects the tab, and raises its window. It is incompatible with
`--background`; successful output is prefixed `trusted `. Text is typed as real input, Unicode
supported. Use it only when a site ignores synthetic events.

`screenshot` requires Screen Recording permission for the terminal. It can capture only the
currently displayed tab of a non-minimized window; it never activates Safari or switches tabs.
Background tabs fail with `browser-cli: tab is not currently displayed; switch to it first
(screenshot cannot capture background tabs)`. A selector captures its first matching element,
clipped to the viewport; a fully offscreen element fails. Safari must also be the active app:
with Stage Manager on, an inactive window is captured as a thumbnail and the command fails
with a "capture does not match the viewport" error instead of returning a wrong image. Use screenshots sparingly because
images cost tokens; prefer text or `snapshot`.

Challenge detection applies to the main frame only; cross-origin iframes are detected by
presence, not inspected. Invisible reCAPTCHA badges count as `none`. `cloudflare-passive` means
a self-clearing interstitial (such as “Just a moment...”) and is waited out silently.
`needs-human` means an interactive Turnstile/reCAPTCHA/hCaptcha widget or Cloudflare block page;
`wait-challenge` reports this to stderr, sends one macOS notification, and waits without touching
the page. On timeout it reports `browser-cli: challenge not cleared after Ns (state: <state>)` and
exits 1.

**Human verification policy:** browser-cli never clicks, types into, reads, or solves CAPTCHA or
human-verification widgets. Agents must not attempt this with `click`, `eval`, `--trusted`, or any
other command. When `needs-human` appears, stop, tell the user, and use `wait-challenge` to resume
after they complete it. Prefer official APIs; in the user's own test environments, use vendor
test keys or disable the challenge.
Quotes, backslashes, and Unicode pass through a UTF-8 temporary file. Shell quoting still
applies. Wrap statements in an immediately invoked function; first/last-line comments work.
Evaluation is synchronous (promises are not awaited), in the page world and main frame only.

## Examples

```sh
browser-cli --browser safari status
browser-cli --browser safari open https://example.com/
browser-cli --browser safari wait --timeout 20 --tab example.com 'document.querySelector("h1") !== null'
browser-cli --browser safari eval --tab example.com '({url: location.href, title: document.title})'
browser-cli --browser safari file --tab 1:2 ./inspect.js
```

## Security

Leaving the permission on lets any program allowed to control the browser run JavaScript in
logged-in pages. Review access in System Settings > Privacy & Security > Automation.
Run only trusted code; do not paste cookies or tokens into logs.
