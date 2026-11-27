# browser-cli

A Bash tool that drives the user's real, logged-in macOS Safari or Google Chrome. Most commands use
system tools only. The user logs in themselves; the tool stores no credentials or page markers.
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

### Chrome background setup

Requires Chrome 120+ and Python 3. From the repository checkout, run:

```sh
./browser-cli --browser chrome install-background
```

The installer selects the sole Chrome profile (or the default for a fresh Chrome). If there are
several, it prompts in a terminal or lists their names and `--profile DIRECTORY` values for a
noninteractive caller. It copies the extension and bridge into `~/Library/Application Support/browser-cli/chrome-background/`,
registers the native messaging host in Chrome, and requests `chrome://extensions/` in the selected
profile. Verify the profile before loading: if Chrome opens a different profile, switch manually.
Turn on **Developer mode**, click **Load unpacked**, and select the extension directory printed by
the command. Chrome requires this final step. Rerun the installer and use **Reload** on the extension to update.

On first use, Chrome `open --background` offers setup in an interactive terminal. Without a terminal,
it reports the setup command and asks the caller to obtain consent first. Setup does not reopen the
requested page. The installer returns after printing the manual loading instructions; it does not
wait for Chrome to connect. After loading the extension, run `install-background --check` (with
`--browser chrome`) to confirm the connection without opening a tab.

To uninstall, remove **browser-cli Background Tabs** from `chrome://extensions/` in every profile
where it was loaded, then run `browser-cli --browser chrome uninstall-background`. The command
removes only the staged files and native host registration, not the CLI or Chrome profile data.
If you never loaded the extension, run the uninstall command directly.

After setup, `browser-cli --browser chrome open --background https://example.com/` reuses a
matching tab or adds an unselected tab without activating Chrome. It searches only within the
profile with the connected extension, accepts HTTP(S) URLs, and requires an existing normal window.
Keep the extension enabled in just one profile: the command fails if none or more than one are
connected. It never falls back to opening in the foreground. The result is `id:<tab-id>` followed
by a tab and `created` or `reused`; use the returned ID with `--tab`.

## Command reference

`browser-cli help` lists the supported commands, options, outputs, and exit codes.

## OpenCode MCP (no skill)

Keep `browser-cli`, `browser_mcp.py`, and `chrome-background/` together on the Mac with the
browser. The server needs Python 3 and uses local stdio, not an HTTP listener. Register it in the
target OpenCode project (add `--global` for a user-wide installation):

```sh
opencode mcp add browser_cli -- /absolute/path/to/browser-cli mcp
opencode mcp list
```

Remove any separately installed `browser-cli` skill from OpenCode discovery if migrating. The
server provides its own instructions and requires no skill. Code Mode exposes one tool:

```js
await tools.browser_cli.read_page({url: "https://example.com/", browser: "safari", include_links: true})
```

- `existing_tab` (default): read an exact matching tab without opening or focusing a browser.
- `session_tab`: use for login-dependent access. In Chrome, first request a candidate without
  `confirmed_tab_id`, then pass that ID only after the user confirms the tab and profile.
- `public_background`: may background-open a missing public URL. Chrome requires the existing
  extension setup; there is no automatic installation, foreground fallback, or tab cleanup.

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
Errors go to stderr as `browser-cli: <message>`. AppleScript failures print
`browser-cli: AppleScript error <code>: <message>` (`unknown` when no error code is available).
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
| Chrome `open --background <url>` | fails without creating a window | not applicable |
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
browser-cli --browser safari status
browser-cli --browser safari open --foreground https://example.com/
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

Allowing JavaScript from Apple Events lets programs with Automation access run JavaScript in
logged-in pages. macOS lists those grants in System Settings > Privacy & Security > Automation.
The Chrome extension reads tab URLs to match the requested prefix. Its native bridge accepts
HTTP(S) open requests from processes running as the same OS user over a private Unix socket;
it does not listen over the network.
