# browser-cli

A dependency-free Bash tool that drives the user's real, logged-in macOS Safari or Google Chrome
through AppleScript. The user logs in themselves; the tool stores no credentials or page markers.
Chrome only (not Edge, Brave, or Chromium). Headless browsers are not supported.

## Setup

Requires macOS and the system Bash/AppleScript tools.

1. Turn on **Allow JavaScript from Apple Events** once by hand: Safari > Settings > Developer
   (needs Touch ID or a password), or Chrome > View > Developer (needs a Chrome window open). Leave it on;
   this tool never changes the setting and never quits or restarts the browser.
2. The first run may prompt macOS to let the calling program control the selected browser; allow it.
3. Copy `browser-cli` to a directory on your PATH and make it executable.

Optional permissions, granted to the terminal in System Settings > Privacy & Security:
Screen Recording for `screenshot`, Accessibility for `--trusted`. See [Permissions](#permissions).

## Commands

Syntax: `browser-cli --browser safari|chrome <command> [options]`. `--browser` is required for every
command below and may appear anywhere in the arguments; without it the tool prints
`browser-cli: --browser safari|chrome is required` and exits 2. `help` and `version` do not need it.
Commands behave the same on both browsers unless [Browser differences](#browser-differences) says otherwise.

| Command | Behavior |
| --- | --- |
| `status` | Probe `1+1` in an existing tab: print `on`, `off`, or `unknown` (no window or tab); never open a window. |
| `tabs [--json]` | List tabs as three TAB-separated fields: `<window>:<tab>`, title, URL. `--json` prints an array of `{window,tab,title,url}` objects. |
| `open <url>` | Focus a tab whose URL starts with the URL, otherwise open one; print `<window>:<tab><TAB>reused` or `<window>:<tab><TAB>created`. |
| `close [--tab T]` | Close the selected tab. |
| `info [--tab T]` | Print title, URL, readyState, body-text/HTML character counts, and a final `challenge: none\|cloudflare-passive\|needs-human` line. |
| `wait-challenge [--tab T] [--timeout N]` | Wait until no challenge page is present; print `challenge: cleared`. Default timeout is 600 seconds. |
| `text [--tab T] [--selector CSS] [--max-chars N]` | Print whitespace-collapsed `innerText`, excluding script/style content. |
| `links [--tab T] [--selector CSS] [--max-chars N]` | Print one `<text><TAB>absolute URL` per link. |
| `html [--tab T] [--selector CSS] [--max-chars N] [--raw]` | Print outerHTML; by default removes script/style/svg/noscript, comments, and inline styles. `--raw` returns unmodified HTML. |
| `snapshot [--tab T] [--selector CSS] [--max-chars N]` | List visible interactive elements as `<index><TAB><tag/role><TAB><label><TAB><selector>`; selector values can be passed to page actions. Password values are never shown. |
| `click [--tab T] [--trusted] <css>` | Click the first matching element; print `clicked <tag> "<text>"`. |
| `fill [--tab T] [--trusted] <css> <text>` | Set a field and dispatch input/change events; print `filled <tag> (<N> chars)`. Native setters support React/Vue inputs. |
| `select [--tab T] <css> <value>` | Select an option by value or label; print `selected "<label>"`. |
| `press [--tab T] [--trusted] <key> [--selector CSS]` | Press Enter, Escape, Tab, arrows, Backspace, Space, or a single character; print `pressed <key>`. Enter in a form submits it. |
| `scroll [--tab T] [--selector CSS] (--to top\|bottom \| --by N)` | Scroll the page or selected element; print `scrollY: <n> of <max>` (`scrollTop` for an element). |
| `submit [--tab T] <css>` | Submit the matching form; print `submitted form`. |
| `eval [--tab T] <js>` | Evaluate an expression. |
| `file [--tab T] <path.js>` | Evaluate an expression read from a UTF-8 file. |
| `wait [--tab T] <js>` | Poll until truthy, printing nothing on success; report the last evaluation error on timeout. |
| `screenshot [--tab T] [--selector CSS] [--out PATH]` | Save a PNG of the visible viewport or first matching element, and print its absolute path. |

Exit codes: 0 success (`status`: on), 1 failure (JavaScript errors, lookup failures, timeout,
unsupported state, or `status` off/unknown), 2 usage error.

## Browser differences

| | Safari | Chrome |
| --- | --- | --- |
| Setting path | Settings > Developer | View > Developer |
| Setting off, error text | `... Turn it on in Safari > Settings > Developer ...` | `... Enable View > Developer > Allow JavaScript from Apple Events ...` (Chrome's own text is localized; the tool matches its help URL) |
| `screenshot` window lookup | Window id equals the system window id. | Matched by window bounds; fails if not unique. |
| Everything else | Same commands, options, output, and exit codes. | |

## Options and results

- `--browser safari|chrome`: required; chooses the user's browser.
- `--tab T`: 1-based `<window>:<tab>` or a URL substring (first match across windows);
  default is the front window's active tab. Indexes may change; prefer URL substrings.
- `--selector CSS`: `text`, `links`, and `html` restrict output to the first matching element;
  `snapshot` filters the elements listed; `screenshot` captures the element; `press` and `scroll`
  target the matching element. A missing match prints `browser-cli: no element matches: <css>`
  to stderr and exits 1.
- `--max-chars N`: `text`, `links`, `html`, and `snapshot` default to 20000 characters. `eval` and `file`
  truncate only when this option is explicitly supplied. Truncation reports the emitted and
  total character counts to stderr and succeeds (exit 0).
- `--raw`: `html` only; preserve the original markup.
- `--json`: `tabs` only; return a JSON array of objects with `window`, `tab`, `title`, and `url`.
- `--to top|bottom` and `--by N`: `scroll` only, exactly one; scroll to an endpoint or by a number of pixels.
- `--background`: `open` only. Do not activate the browser, raise a window, or switch the visible tab.
  Existing match reports `reused`; otherwise add a tab in window 1 and report `created`,
  restoring the previous current tab. Close only tabs reported as `created` by your own `open`.
  Browser not running: launch it in the background. No windows: a new window is still created.
- `--trusted`: `click`, `fill`, `press` only; see [Real input](#real-input). Incompatible with `--background`.
- `--timeout N`: positive integer seconds for a command, default 30; wait polls share one deadline.
  `wait-challenge` defaults to 600 seconds.
- `--out PATH`: `screenshot` only; save to this path. By default, save as `browser-cli-<epoch>.png` in the temp directory.
- `--`: end option parsing.

Strings are printed exactly, including trailing newlines; other values print JSON plus a
newline, and `undefined` prints an empty line. Shell command substitution strips trailing
newlines, so redirect to a file for exact comparisons.
In `tabs` output, titles and URLs have embedded tabs and newlines replaced with spaces.
Errors go to stderr as `browser-cli: <message>`. AppleScript failures print
`browser-cli: AppleScript error <code>: <message>` (`unknown` when no error code is available).
Quotes, backslashes, and Unicode pass through a UTF-8 temporary file. Shell quoting still
applies. Wrap statements in an immediately invoked function; first/last-line comments work.
Evaluation is synchronous (promises are not awaited), in the page world and main frame only.

## No window or no tab

The browser process can keep running with nothing open. There is nothing to read, so only commands
that need a tab fail. Two states exist:

- **Zero windows** (Safari and Chrome): for example after closing the last window.
- **A window with zero tabs** (Safari only): closing the last tab of the last window can leave an
  empty window behind. Chrome closes the window together with its last tab, so it never has this state.

| Command | Zero windows | Safari window with zero tabs |
| --- | --- | --- |
| `status` | prints `unknown`, exit 1 | same |
| `tabs` | prints nothing, exit 0 (`--json` prints `[]`) | same; empty output means no tabs, not a broken tool |
| `open <url>` | creates a window, prints `1:1<TAB>created` | same (a new window replaces the empty one) |
| other commands | `browser-cli: AppleScript error -2700: no <Browser> window is open`, exit 1 | `AppleScript error -1728` (localized text), exit 1 |
| `wait`, `wait-challenge` | keep retrying; fail only at the timeout (`wait-challenge`: `state: unknown`) | same |

Recovery in both states: run `open <url>`, then `status`.

## Real input

Default page actions run inside the page via JavaScript, so dispatched events are synthetic
(`isTrusted` is false). They do not activate the browser, switch tabs, or require extra
permissions, and work with `--background`. Some sites (including strong anti-bot and some
payment pages) ignore synthetic events: the command reports what it dispatched, but the page may
not react. Verify with `text`, `snapshot`, or `wait`; do not retry blindly.

`--trusted` on `click`, `fill`, or `press` sends real OS-level input (`isTrusted=true`). It
requires Accessibility permission, activates the selected browser, selects the tab, and raises its
window, so it is incompatible with `--background`. Successful output is prefixed `trusted `, for
example `trusted clicked button "Go"`. Details:

- `click` posts a real mouse click at the element center after verifying the page viewport position;
  if the position cannot be verified it fails without clicking.
- `fill` selects the existing content with Cmd+A and pastes the text, so Unicode works. It
  temporarily uses the clipboard and restores it afterwards.
- `press` supports the named keys and printable ASCII only; use `fill` for Unicode text.
- Use it only when a site ignores synthetic events.

**Do not use the browser while a `--trusted` command runs** (it takes about a second). Before each
click, paste, or key it checks that the browser is frontmost and that the target window and tab are
still the ones it selected. If you switch app, switch tab, or close the tab or window, it stops with
`browser-cli: the target Safari tab is no longer in front; nothing more was sent` or
`browser-cli: the target tab was closed or changed during the action; nothing more was sent`
(exit 1). The clipboard is restored.

Known gap: the target tab is first found by URL substring and its identity is read a moment later.
If the user closes the target in that instant and a neighbouring tab takes its position, input can
go to the neighbour while the command reports success. The launcher re-checks the tab's URL against
the `--tab` text to narrow this. It was confirmed to stop the case on Safari. It was not confirmed on
Chrome, where it occurred before the re-check was added. It cannot help when `--tab` is an index
such as `1:2` or omitted, or when the neighbour's URL also contains the same text. Prefer a specific
URL substring and avoid closing tabs while the command runs.

## Screenshots

`screenshot` requires Screen Recording permission for the terminal. It can capture only the
currently displayed tab of a non-minimized window; it never activates the browser or switches tabs.
Background tabs fail with `browser-cli: tab is not currently displayed; switch to it first
(screenshot cannot capture background tabs)`. A selector captures its first matching element,
clipped to the viewport; a fully offscreen element fails with `element is outside the viewport`
(scroll first), and a hidden element (no layout box, `visibility:hidden`, or zero opacity) fails
with `element is not visible`.

The browser must be the active app: with Stage Manager on, an inactive browser window is captured
as a thumbnail and the command fails with a "capture does not match the viewport" error instead of
returning a wrong image. A size mismatch is retried for about 3 seconds so a capture right after a
tab or app switch succeeds; a window that stays inactive then fails. Use screenshots sparingly
because images cost tokens; prefer `text` or `snapshot`.

## Challenge pages

`info` reports `challenge: none|cloudflare-passive|needs-human`. Detection applies to the main
frame only; cross-origin iframes are detected by presence, not inspected. Invisible reCAPTCHA
badges count as `none`. `cloudflare-passive` means a self-clearing interstitial (such as
"Just a moment...") and is waited out silently. `needs-human` means an interactive
Turnstile/reCAPTCHA/hCaptcha widget or a Cloudflare block page.

`wait-challenge` polls until the state is `none`. On `needs-human` it prints
`browser-cli: human verification needed on <host>; waiting for you to complete it` to stderr,
sends one macOS notification, and keeps waiting without touching the page. On timeout it prints
`browser-cli: challenge not cleared after Ns (state: <state>)` and exits 1.

**Human verification policy:** browser-cli never clicks, types into, reads, or solves CAPTCHA or
human-verification widgets. Agents must not attempt this with `click`, `eval`, `--trusted`, or any
other command. When `needs-human` appears, stop, tell the user, and use `wait-challenge` to resume
after they complete it. Prefer official APIs; in the user's own test environments, use vendor
test keys or disable the challenge.

## Permissions

| Permission (granted to the terminal) | Needed for | When missing |
| --- | --- | --- |
| Automation: control the browser | every command | macOS prompts on first use and the command waits (up to `--timeout`); if previously denied, an `AppleScript error <code>: ...` (macOS reports -1743) with no special hint; enable it in the Automation list |
| Allow JavaScript from Apple Events (browser setting) | every JavaScript-backed command | clear error naming the setting; `status` prints `off` |
| Screen Recording | `screenshot` | `screen capture failed` or `screen capture is empty or black`, pointing at System Settings |
| Accessibility | `--trusted` | `--trusted needs Accessibility permission for your terminal ...` |
| Notifications | `wait-challenge` alert | the alert is skipped; the command still waits |

Permissions are per application: a different terminal or host program needs its own grants.

## Examples

```sh
browser-cli --browser safari status
browser-cli --browser safari open --background https://example.com/
browser-cli --browser safari wait --timeout 20 --tab example.com 'document.querySelector("h1") !== null'
browser-cli --browser safari eval --tab example.com '({url: location.href, title: document.title})'
browser-cli --browser chrome info --tab example.com
browser-cli --browser chrome snapshot --tab example.com --max-chars 4000
browser-cli --browser chrome fill --tab example.com 'input[name="q"]' 'hello'
browser-cli --browser chrome press --tab example.com Enter --selector 'input[name="q"]'
browser-cli --browser chrome screenshot --tab example.com --selector '#chart' --out /tmp/chart.png
browser-cli --browser safari file --tab 1:2 ./inspect.js
```

## Security

Leaving the permission on lets any program allowed to control the browser run JavaScript in
logged-in pages. Review access in System Settings > Privacy & Security > Automation.
Run only trusted code; do not paste cookies or tokens into logs.
