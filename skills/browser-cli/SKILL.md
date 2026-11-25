---
name: browser-cli
description: Drives the user's real, logged-in macOS Safari or Chrome through the `browser-cli` Bash tool. Use for reading pages behind a login, running JavaScript in a tab, listing/opening/closing tabs, or scraping sites where fetch tools fail because of login or anti-bot checks.
---

# browser-cli

Controls the user's real Safari or Google Chrome via AppleScript. Run `browser-cli help` for command syntax and output details.

## Preconditions

- macOS only. The script ships with this skill: below, `browser-cli` means `./browser-cli`, relative to the directory containing this file (the skill base directory given when the skill loads).
- Ask the user which browser holds the logged-in session; do not infer it.
- Chrome cannot tell which profile a window belongs to. For a Chrome login, run `tabs --json`, find a tab already on the target site, tell the user which tab you will use (title and URL, not the whole list), and continue after they confirm. If no such tab exists, ask the user to open the logged-in page themselves in the profile that has the login; do not use `open`, which creates the tab in window 1 and may target another profile. After confirmation, pin that tab with `--tab id:<id>` using its `id` from `tabs --json` for all later commands. If the id stops matching, the tab was closed or Chrome restarted; ask the user again. Safari has no tab id; use a specific URL substring.
- Run `browser-cli --browser <safari|chrome> status` first, substituting the selected browser.
  - `on`: proceed.
  - `off`: stop and ask the user to enable Safari > Settings > Developer or Chrome > View > Developer > "Allow JavaScript from Apple Events" (needs Touch ID or a password). Never change this setting, and never quit or restart the browser.
  - `unknown`: no window or tab is open (the browser process may be running with zero windows). Run `browser-cli --browser <safari|chrome> open <url>`, then `status` again.
- Empty `tabs` output (exit 0; `--json` gives `[]`) means there are no tabs. A command that needs a tab cannot run until a URL is opened. If a tab command fails with `AppleScript error -1728` (macOS wording, possibly localized), the window has no tab: run `open <url>` first.

## Workflow

1. Use `open --background` unless the next steps need `screenshot` or `--trusted` (they need the tab in the foreground). `open` reports `reused` or `created`; close only a tab reported `created` by your own `open`.
2. `browser-cli --browser <safari|chrome> wait --tab <url-substring> '<js condition>'` until the page is ready (default timeout 30 s; `--timeout N` to change).
3. For page reading, use `info` -> `text`/`links` -> `snapshot`, then `html`/`screenshot`/`eval`. Prefer `text`/`snapshot` over `screenshot`; use `--selector` and a small `--max-chars`.
4. For interaction, use `snapshot` to find targets, then an action (`click`, `fill`, `select`, `press`, or `submit`), then `wait` or `text`/`snapshot` to verify the result. If the page does not react to a click or fill, try once with `--trusted` and tell the user it brings the browser to the front. Prefer `snapshot` over `html` to save tokens.
5. Use `info` and `wait-challenge` when a page looks like a verification page. When `info` shows `challenge: needs-human`, stop, tell the user, and continue with `wait-challenge` after they complete it. `cloudflare-passive` is waited out by `wait-challenge`. `wait-challenge` defaults to 600 s; if it times out, stop and tell the user.
6. `screenshot` needs the target tab to be the displayed tab of the active browser (it never switches tabs).
7. Use `browser-cli --browser <safari|chrome> eval --tab <url-substring> '<js expression>'` for custom reads. Use `file` for long scripts.
8. `browser-cli --browser <safari|chrome> close --tab <url-substring>` only for tabs reported `created` by your own `open`.

## Rules

- When a command fails, stop and tell the user the error; do not run the same command again. If the message names a next step (for example `tabs`, `snapshot`, or `open <url>`), running that step first is allowed; after that diagnostic, report the result instead of running the original command again.
- For Safari, select tabs by a specific URL substring because `<window>:<tab>` indexes change; for Chrome, prefer `--tab id:<id>`.
- `eval` is synchronous: promises are not awaited. Return plain values and poll with `wait` for async results.
- Default page actions dispatch synthetic JavaScript events (`isTrusted` is false); some sites ignore them. `--trusted` is available for `click`, `fill`, and `press`; it sends real OS-level input, activates the target browser, uses the clipboard briefly for `fill`, and requires Accessibility permission. `press --trusted` takes named keys or printable ASCII only. Tell the user before running it and ask them not to touch the browser for a few seconds. Switching app or tab, or closing the tab, aborts it with `nothing more was sent` (exit 1); do not retry automatically. A known gap is the short interval between the final identity check and OS event delivery.
- browser-cli never clicks, types into, reads, or solves CAPTCHA/human-verification widgets. When `info` shows `challenge: needs-human`, stop, tell the user, and continue with `wait-challenge` after they complete it. `cloudflare-passive` is waited out by `wait-challenge`; `wait-challenge` defaults to 600 s. If it times out, stop and tell the user.
- Wrap multi-statement code in an immediately invoked function that returns the value.
- Return only what is needed (selected fields, not whole DOM or storage).
- Strings print as-is; other values print as JSON. Redirect to a file when exact trailing newlines matter.
- Exit codes: 0 ok, 1 failure (JS error, no tab, timeout, status off/unknown), 2 usage error.

## Safety

- The browser session is the user's. Do not submit forms, post, purchase, delete, or change account settings unless the user asked for that action.
- Never print or log cookies, tokens, or `localStorage` secrets.
- Run only code you wrote or trust.
