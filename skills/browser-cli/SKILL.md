---
name: browser-cli
description: Drives the user's real, logged-in macOS Safari or Chrome through the `browser-cli` Bash tool. Use for reading pages behind a login, running JavaScript in a tab, listing/opening/closing tabs, or scraping sites where fetch tools fail because of login or anti-bot checks.
---

# browser-cli

macOS only. Use the bundled script by its absolute path: replace `<skill base directory>` with the directory shown when this skill loads. For command syntax, run `"<skill base directory>/browser-cli" help`.

## 1. Select a browser and tab

- If the task needs the user's login, ask which browser holds it unless the user already said. Do not guess. Every browser command requires `--browser safari` or `--browser chrome`.
- Start with `"<skill base directory>/browser-cli" --browser <browser> status`. On `off`, stop and ask the user to enable **Allow JavaScript from Apple Events** in Safari > Settings > Developer or Chrome > View > Developer. Never change that setting or restart the browser.
- On `unknown`: for a public page, use `open` with the mode described below; for a page that needs the user's login, ask them to open it in the correct browser and profile. Once the page is open, check `status` again.
- If a tab command fails with `-1728`, check `tabs`. If there are no tabs, follow the `unknown` recovery above. If tabs exist or the check fails, report the original error and stop. Do not retry the failed tab command automatically.
- Identify the tab with `tabs`. For a Chrome login, use `tabs --json` and confirm the candidate's title and URL with the user before proceeding: the tool cannot identify Chrome profiles. If the user opens the page, identify and confirm that tab the same way. On Chrome, pin the chosen tab with `--tab id:<id>` using its `id` from `tabs --json`. On Safari, use `--tab <specific-url-substring>` because `<window>:<tab>` indexes can change. Reconfirm the target if the selector no longer matches; do not silently switch tabs.
- If the target is missing, ask the user to open login pages in the correct browser and profile; never use `open` for those pages. For public pages, pass exactly one mode: `open --background <url>` for ordinary work, or `open --foreground <url>` if a screenshot needs the tab visible. For an existing tab that needs a screenshot, ask the user to bring it into view rather than opening another tab.

## 2. Check for verification

Run `info` on the selected tab before reading or interacting. If a human-verification page is visible or `info` reports `challenge: needs-human`, stop and tell the user. After they complete it, use `wait-challenge` to confirm it cleared. For `cloudflare-passive`, use `wait-challenge` directly. Its default timeout is 600 seconds; if it times out, report it and stop. Never click, type into, read, or solve verification widgets.

## 3. Read or interact

- Start with `text` or `links` for reading, and `snapshot` for finding controls. Use `html`, `screenshot`, or `eval` only as needed. Scope reads with `--selector` or `--max-chars` where supported. `screenshot` captures the displayed tab of the active browser; it does not switch tabs.
- Act on the pinned target and verify the result with `wait`, `text`, or `snapshot`. `eval` is synchronous; use `wait` for an asynchronous condition. Run only code you wrote or trust, return only needed data, and never print or log cookies, tokens, or storage secrets.
- If successful synthetic input has no effect, try `--trusted` once for `click`, `fill`, or `press`. It brings the browser to the front and selects the target tab without a separate `open --foreground`. It requires Accessibility permission and briefly uses the clipboard for `fill`. Before sending real input, tell the user and ask them not to touch the browser. If the target changes and the command reports `nothing more was sent`, stop and report it; do not retry automatically.
- Do not submit forms, post, purchase, delete, or change account settings unless the user asked. On command failure, stop and report it. You may follow a next step named in the error if it respects the login and verification rules above; report the result instead of rerunning the original command.

## 4. Close only what you created

Use `close --tab <target>` only for the same tab that your `open` reported as `created`, never for a `reused` or user-opened tab. If you cannot identify the original tab, leave it open. Closing a window's only tab removes the window in Chrome and can leave an empty window in Safari.
