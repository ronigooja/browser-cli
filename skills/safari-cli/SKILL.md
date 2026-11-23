---
name: safari-cli
description: Use when a task needs the user's real, logged-in macOS Safari - reading pages behind a login, running JavaScript in an open tab, listing/opening/closing Safari tabs, or scraping sites where fetch tools fail because of login or anti-bot checks. Drives Safari through the `safari-cli` Bash tool.
---

# safari-cli

Controls the user's real Safari via AppleScript. Command reference, options, and output rules: `README.md` or `safari-cli help`. Do not duplicate them here.

## Preconditions

- macOS only. `safari-cli` must be on PATH; if not, run `/Users/zhuwei/Terminal/safari-cli/safari-cli` directly.
- Run `safari-cli status` first.
  - `on`: proceed.
  - `off`: stop and ask the user to enable Safari > Settings > Developer > "Allow JavaScript from Apple Events" (needs Touch ID or a password). Never change this setting, and never quit or restart Safari.
  - `unknown`: no window or tab is open. Run `safari-cli open <url>`, then `status` again.

## Workflow

1. `safari-cli open <url>` focuses an existing tab or opens a new one.
2. `safari-cli wait --tab <url-substring> '<js condition>'` until the page is ready.
3. `safari-cli eval --tab <url-substring> '<js expression>'` to read data. Use `file` for long scripts.
4. `safari-cli close --tab <url-substring>` only for tabs you opened.

## Rules

- Select tabs by URL substring; `<window>:<tab>` indexes change.
- `eval` is synchronous: promises are not awaited. Return plain values and poll with `wait` for async results.
- Wrap multi-statement code in an immediately invoked function that returns the value.
- Return only what is needed (selected fields, not whole DOM or storage).
- Strings print as-is; other values print as JSON. Redirect to a file when exact trailing newlines matter.
- Exit codes: 0 ok, 1 failure (JS error, no tab, timeout, status off/unknown), 2 usage error.

## Safety

- The Safari session is the user's. Do not submit forms, post, purchase, delete, or change account settings unless the user asked for that action.
- Never print or log cookies, tokens, or `localStorage` secrets.
- Run only code you wrote or trust.
