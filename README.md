# agent-browser

A Bash tool that drives the user's real, logged-in macOS Safari or Google Chrome. Most commands use
system tools only. The user logs in themselves; the tool stores no credentials or page markers.
Chrome only (not Edge, Brave, or Chromium). Headless browsers are not supported.

## Setup

Requires macOS and the system Bash/AppleScript tools.

1. Turn on **Allow JavaScript from Apple Events** once by hand: Safari > Settings > Developer
   (needs Touch ID or a password), or Chrome > View > Developer (needs a Chrome window open). Leave it on;
   this tool never changes the setting and never quits or restarts the browser.
2. The first run may prompt macOS to let the calling program control the selected browser; allow it.
3. Copy `agent-browser` to a directory on your PATH and make it executable.

Optional permissions, granted to the terminal in System Settings > Privacy & Security:
Screen Recording for `screenshot`, Accessibility for `--trusted`. See [Permissions](#permissions).

### Chrome background setup

Requires Chrome 120+ and Python 3. From the repository checkout, run:

```sh
./agent-browser --browser chrome install-background
```

The installer selects the sole Chrome profile (or the default for a fresh Chrome). If there are
several, it prompts in a terminal or lists their names and `--profile DIRECTORY` values for a
noninteractive caller. It copies the extension and bridge into `~/Library/Application Support/agent-browser/chrome-background/`,
registers the native messaging host in Chrome, and requests `chrome://extensions/` in the selected
profile. Verify the profile before loading: if Chrome opens a different profile, switch manually.
Turn on **Developer mode**, click **Load unpacked**, and select the extension directory printed by
the command. Chrome requires this final step. Rerun the installer and use **Reload** on the extension to update.

On first use, Chrome `open --background` offers setup in an interactive terminal. Without a terminal,
it reports the setup command and asks the caller to obtain consent first. Setup does not reopen the
requested page. The installer returns after printing the manual loading instructions; it does not
wait for Chrome to connect. After loading the extension, run `install-background --check` (with
`--browser chrome`) to confirm the connection without opening a tab.

To uninstall, remove **Agent Browser** from `chrome://extensions/` in every profile
where it was loaded, then run `agent-browser --browser chrome uninstall-background`. The command
removes only the staged files and native host registration, not the CLI or Chrome profile data.
If you never loaded the extension, run the uninstall command directly.

After setup, `agent-browser --browser chrome open --background https://example.com/` reuses a
tab with the exact requested URL or adds an unselected tab without activating Chrome. It searches only within the
profile with the connected extension and accepts HTTP(S) URLs. If that profile has no normal
window, the extension creates one without requesting focus. Its first tab is selected in the new
window; it does not change any existing window's selected tab.
For MCP public reads only, if Chrome is running with zero windows and exactly one verified
regular profile has the extension installed, one background profile wake is attempted before
opening the URL. If reconnection fails, the tool stops without sending the URL or falling back
to a foreground open. A connection check never wakes Chrome.
Keep the extension enabled in just one profile: the command fails if none or more than one are
connected. It never falls back to opening in the foreground. The result is `id:<tab-id>` followed
by a tab and `created` or `reused`; use the returned ID with `--tab`.

## Command reference

`agent-browser help` lists the supported commands, options, outputs, and exit codes.

## OpenCode MCP (no skill)

Keep `agent-browser`, `agent_browser_mcp.py`, `agent_browser_mcp_setup.py`, and `chrome-background/` together on the Mac with the
browser. The server needs Python 3 and uses local stdio, not an HTTP listener. Register it in the
target OpenCode project (add `--global` for a user-wide installation):

```sh
opencode mcp add agent_browser -- /absolute/path/to/agent-browser mcp
opencode mcp list
```

If upgrading from the legacy skill, move its installed directory out of OpenCode skill discovery
(typically `~/.config/opencode/skills/browser-cli/`; this is intentionally the historical legacy path).
The skill is no longer shipped; the server
provides its own instructions. Start with `read_page`:

```js
await tools.agent_browser.read_page({url: "https://example.com/", browser: "safari", include_links: true})
```

- `public_background` (default): reuse a matching tab or background-open a missing public URL.
  If no browser is specified, ask only which browser to use. Chrome requires the extension and
  never falls back to a foreground open.
- `existing_tab`: read a matching tab without opening or focusing a browser.
- `session_tab`: use for login-dependent access. In Chrome, first request a candidate without
  `confirmed_tab_id`, then pass that ID only after the user confirms the tab and profile.
  Ask the user to open a missing tab in the correct browser/profile; do not open it for them.

Safari requires a unique URL substring match, then verifies the selected document URL at read
time. Chrome pins the tab by ID. `selector` scopes returned text and links, but not the login
or verification check. The server checks browser permission before reading and waits up to
600 seconds for a passive challenge; human verification always stops the read.

For asynchronously rendered content, set `wait_for_selector` to a CSS selector that identifies
the requested content. MCP checks for any visible match on the same tab for up to eight seconds
after target selection, independently of `selector`, and reports `content_not_ready` on expiry.
For example, an X timeline read can use `wait_for_selector: 'main a[href*="/status/"]'` and
`selector: 'main'`; the condition does not establish that the full timeline has loaded.

`url_policy` defaults to `exact`. Use `allow_added_query` only if extra query parameters are
acceptable for the task: the origin, path, fragment, and every requested query pair (including
duplicate counts) must remain unchanged. Chrome follows only the original tab ID; a mismatch
reports its reason without returning page content or following a different destination.
If a mismatch offers a `resume_token`, retry the original URL with the suggested policy and
that token in the same session. This pins the retry to the original Chrome tab even if other
tabs now match. The cleanup token remains valid until used or expired.

Chrome background setup requires explicit user approval. Use `setup_chrome_background` to
`prepare` after approval, `install` with the returned grant and selected profile, and `check`
after the user loads it. Relay the returned instructions in full. It never reinstalls a pending or
disconnected extension. When done with a browser task, call `cleanup_tab` only with a token
returned by `read_page`. Tokens identify only Chrome tabs created by that call and are scoped
to the originating session. Reused tabs and Safari tabs without a stable ID have no token.

Non-`ok` statuses identify blockers without returning page text. Login and verification detection
are heuristic; empty results, timeouts, and navigation changes are not proof that sign-in is needed.
Text is limited to 12,000 UTF-16 code units; links are limited to 25 visible HTTP(S) links and
8,000 combined label/URL code units. The server does
not expose arbitrary JavaScript, shell, form actions, or browser storage.

## Browser differences

| | Safari | Chrome |
| --- | --- | --- |
| Setting path | Settings > Developer | View > Developer |
| Profiles | Not applicable. | `open --background` uses the connected extension's profile. AppleScript selectors span all profiles; `open --foreground` creates new tabs in window 1. |
| `screenshot` window lookup | Window id equals the system window id. | Matched by window bounds; fails if not unique. |

## Implementation details

- `--browser safari|chrome`: required; chooses the user's browser.
- Indexed tab selectors can change when windows or tabs are reordered.
- `--selector CSS`: `text`, `links`, and `html` restrict output to the first matching element;
  `snapshot` filters the elements listed; `screenshot` captures the element; `press` and `scroll`
  target the matching element.
- Truncation reports the emitted and total character counts to stderr without failing the command.
- Safari background creation restores the previous tab selection when adding to an existing window.
- Wait polls share one deadline.

Strings are printed exactly, including trailing newlines; other values print JSON plus a
newline, and `undefined` prints an empty line. Shell command substitution strips trailing
newlines, so redirect to a file for exact comparisons.
In `tabs` output, titles and URLs have embedded tabs and newlines replaced with spaces.
Errors go to stderr as `agent-browser: <message>`. AppleScript failures print
`agent-browser: AppleScript error <code>: <message>` (`unknown` when no error code is available).
Quotes, backslashes, and Unicode pass through a UTF-8 temporary file. Shell quoting still
applies. Wrap statements in an immediately invoked function; first/last-line comments work.
Evaluation runs in the page world and main frame only.

## No window or no tab

The browser process can keep running with nothing open. There is nothing to read, so only commands
that need a tab fail. Two states exist:

- **Zero windows** (Safari and Chrome): for example after closing the last window.
- **A window with zero tabs** (Safari only): closing the last tab of the last window can leave an
  empty window behind. Chrome closes the window together with its last tab, so it never has this state.

| Command | Zero windows | Safari window with zero tabs |
| --- | --- | --- |
| `open --foreground <url>` or Safari `open --background <url>` | creates a window | a new window replaces the empty one |
| Chrome `open --background <url>` | connected extension creates an inactive window | not applicable |
| other tab commands | fail with a no-window error | can fail with localized AppleScript error `-1728` |
| `wait`, `wait-challenge` | poll until timeout because there is no tab to probe | same |

## Real input

Default page actions run inside the page via JavaScript, so dispatched events are synthetic
(`isTrusted` is false). They do not activate the browser, switch tabs, or require extra
permissions. Some sites ignore synthetic events: the command reports what it dispatched, but the
page may not react.

`--trusted` on `click`, `fill`, or `press` sends real OS-level input (`isTrusted=true`). It
requires Accessibility permission, activates the selected browser, selects the tab, and raises its
window, so it is incompatible with `--background`. Successful output is prefixed `trusted `, for
example `trusted clicked button "Go"`. Details:

- `click` posts a real mouse click at the element center after verifying the page viewport position;
  if the position cannot be verified it fails without clicking.
- `fill` selects the existing content with Cmd+A and pastes the text, so Unicode works. It
  temporarily uses the clipboard and restores it afterwards.
- `press` supports named keys and printable ASCII only.

Before each click, paste, or key it checks that the browser is frontmost and that the target window and tab are
still the ones it selected. If the app, tab, or window changes, it aborts further input and restores
the clipboard.

Known gaps (input going to a neighbouring tab if the target is closed at the wrong instant):

- **Safari** tabs have no id. The tab is found by `--tab`, then identified by a URL+title
  fingerprint, and the launcher re-checks that its URL still contains the `--tab` text. This stops the
  case where the target is closed and a neighbour takes its index; it was verified with a
  deterministic pause. It cannot help when `--tab` is an index such as `1:2` or omitted, or when the
  neighbour has the same URL and title.
- **Chrome** reads each candidate's URL and tab id together in one call and then uses the id, not
  the index, for selection and for every check before input. This removes the separate
  "find by URL, then read the id" step where input once reached a neighbour. Tested once with a
  deterministic pause right after the id was read: the target tab was closed, its neighbour took its
  position, and `fill --trusted` exited 1 with `the target tab was closed or changed during the
  action; nothing more was sent` while the neighbour's input stayed empty. Not tested: closing the
  tab during the single call that matches the URL and reads the id (the code has no separable step
  there, so this rests on reading it, not on a test), and `--tab` given as an index or omitted.
- Both: a very short window remains between the last identity check and the operating system
  delivering the event; it cannot be removed.

## Screenshots

`screenshot` requires Screen Recording permission for the terminal. It can capture only the
currently displayed tab of a non-minimized window; it never activates the browser or switches tabs.
A selector captures its first matching element, clipped to the viewport. Fully offscreen and
hidden elements (no layout box, `visibility:hidden`, or zero opacity) fail.

The browser must be the active app: with Stage Manager on, an inactive browser window is captured
as a thumbnail and the command fails with a "capture does not match the viewport" error instead of
returning a wrong image. The command retries a size mismatch for about 3 seconds; a persistent
mismatch fails.

## Challenge pages

`info` reports `challenge: none|cloudflare-passive|needs-human`. Detection applies to the main
frame only; cross-origin iframes are detected by presence, not inspected. Invisible reCAPTCHA
badges count as `none`. `cloudflare-passive` means a self-clearing interstitial (such as
"Just a moment...") and is waited out silently. `needs-human` means an interactive
Turnstile/reCAPTCHA/hCaptcha widget or a Cloudflare block page.

`wait-challenge` waits while a passive challenge is detected and prints `challenge: none` when no
known challenge is detected. On `needs-human` it stops with an error and attempts one macOS
notification without interacting with the widget.

## Permissions

| Permission (granted to the terminal) | Needed for | When missing |
| --- | --- | --- |
| Automation: control the browser | AppleScript-backed commands | macOS prompts on first use; a previously denied request surfaces as a macOS AppleScript error |
| Allow JavaScript from Apple Events (browser setting) | JavaScript-backed commands | the diagnostic names the setting |
| Screen Recording | `screenshot` | capture fails |
| Accessibility | `--trusted` | real input is not sent |
| Notifications | `wait-challenge` alert | the alert is skipped; the error is still printed |

Permissions are per application: a different terminal or host program needs its own grants.

## Examples

```sh
agent-browser --browser safari status
agent-browser --browser safari open --foreground https://example.com/
agent-browser --browser safari wait --timeout 20 --tab example.com 'document.querySelector("h1") !== null'
agent-browser --browser safari eval --tab example.com '({url: location.href, title: document.title})'
agent-browser --browser chrome info --tab example.com
agent-browser --browser chrome snapshot --tab example.com --max-chars 4000
agent-browser --browser chrome fill --tab example.com 'input[name="q"]' 'hello'
agent-browser --browser chrome press --tab example.com Enter --selector 'input[name="q"]'
agent-browser --browser chrome screenshot --tab example.com --selector '#chart' --out /tmp/chart.png
agent-browser --browser safari file --tab 1:2 ./inspect.js
```

## Security

Allowing JavaScript from Apple Events lets programs with Automation access run JavaScript in
logged-in pages. macOS lists those grants in System Settings > Privacy & Security > Automation.
The Chrome extension reads tab URLs to match the exact requested URL. Its native bridge accepts
HTTP(S) open requests from processes running as the same OS user over a private Unix socket;
it does not listen over the network.
