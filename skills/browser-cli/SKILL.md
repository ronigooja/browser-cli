---
name: browser-cli
description: Drives the user's real, logged-in macOS Safari or Chrome through the `browser-cli` Bash tool. Use for reading pages behind a login, running JavaScript in a tab, listing/opening/closing tabs, or scraping sites where fetch tools fail because of login or anti-bot checks.
---

# browser-cli

Controls the user's real Safari or Google Chrome via AppleScript. Command reference, options, and output rules: `README.md` or `browser-cli help`. Do not duplicate them here.

## Preconditions

- macOS only. The script ships with this skill: below, `browser-cli` means `./browser-cli`, relative to the directory containing this file (the skill base directory given when the skill loads).
- Ask or infer which browser holds the user's logged-in session; never guess silently.
- Run `browser-cli --browser <safari|chrome> status` first, substituting the selected browser.
  - `on`: proceed.
  - `off`: stop and ask the user to enable Safari > Settings > Developer or Chrome > View > Developer > "Allow JavaScript from Apple Events" (needs Touch ID or a password). Never change this setting, and never quit or restart the browser.
  - `unknown`: no window or tab is open. Run `browser-cli --browser <safari|chrome> open <url>`, then `status` again.

## Workflow

1. `browser-cli --browser <safari|chrome> open <url>` focuses an existing tab or opens a new one. Add `--background` to avoid stealing focus or switching the user's visible tab. It reports `reused` or `created`; close only a tab reported `created` by your own `open`.
2. `browser-cli --browser <safari|chrome> wait --tab <url-substring> '<js condition>'` until the page is ready.
3. For page reading, use this order to limit output and save tokens: `info` first to gauge size, then `text` or `links`; use `html` only when structure or selectors are needed; use `eval` for custom needs. Prefer `--selector` and a small `--max-chars`.
4. For interaction, use `snapshot` to find targets, then an action (`click`, `fill`, `select`, `press`, or `submit`), then `wait` or `text`/`snapshot` to verify the result. If the page does not react to a click or fill, try once with `--trusted` on Safari and tell the user it brings Safari to the front. Prefer `snapshot` over `html` to save tokens.
5. Use `info` and `wait-challenge` when a page looks like a verification page. If `needs-human` appears, stop, tell the user, and wait for them to complete verification before resuming.
6. Use `browser-cli --browser <safari|chrome> eval --tab <url-substring> '<js expression>'` for custom reads. Use `file` for long scripts.
7. `browser-cli --browser <safari|chrome> close --tab <url-substring>` only for tabs reported `created` by your own `open`.

## Rules

- Select tabs by URL substring; `<window>:<tab>` indexes change.
- `eval` is synchronous: promises are not awaited. Return plain values and poll with `wait` for async results.
- Default page actions dispatch synthetic JavaScript events (`isTrusted` is false); some sites ignore them. `--trusted` is available for `click`, `fill`, and `press` in Safari when needed; it sends real OS-level input and brings Safari to the front. Use it only after a synthetic action fails to work.
- browser-cli never clicks, types into, reads, or solves CAPTCHA/human-verification widgets; agents must not try with `click`, `eval`, `--trusted`, or any other command. For `needs-human`, stop, tell the user, and use `wait-challenge` to resume after they complete verification. Prefer official APIs and, for the user's own test environments, vendor test keys or disabling the challenge.
- Wrap multi-statement code in an immediately invoked function that returns the value.
- Return only what is needed (selected fields, not whole DOM or storage).
- Strings print as-is; other values print as JSON. Redirect to a file when exact trailing newlines matter.
- Exit codes: 0 ok, 1 failure (JS error, no tab, timeout, status off/unknown), 2 usage error.

## Safety

- The browser session is the user's. Do not submit forms, post, purchase, delete, or change account settings unless the user asked for that action.
- Never print or log cookies, tokens, or `localStorage` secrets.
- Run only code you wrote or trust.
