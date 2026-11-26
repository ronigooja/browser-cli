---
name: browser-cli
description: Drives the user's real, logged-in macOS Safari or Chrome through the `browser-cli` Bash tool. Use for reading pages behind a login, running JavaScript in a tab, listing/opening/closing tabs, or scraping sites where fetch tools fail because of login or anti-bot checks.
---

# browser-cli

Run `"<skill base directory>/browser-cli" help` for syntax and output. Substitute the absolute base directory shown when this skill loads. Use that script path for every call rather than looking up `browser-cli` on `PATH`.

## 1. Select a browser and tab

- If the task needs the user's login, ask which browser holds it unless the user already said. Do not guess.
- Start with `"<skill base directory>/browser-cli" --browser <browser> status`. On `off`, report the permission diagnostic and ask the user to enable the setting. Do not change it or restart the browser. On `unknown`, use the missing-target rule below, then check `status` again once the page is open.
- If a tab command fails with `-1728`, check `tabs`. If there are no tabs, follow the `unknown` recovery above. If tabs exist or the check fails, report the original error and stop. Do not retry the failed tab command automatically.
- Identify the tab with `tabs`. For a Chrome login, use `tabs --json` and confirm the candidate's title and URL with the user before proceeding: the tool cannot identify Chrome profiles. If the user opens the page, identify and confirm that tab the same way. On Chrome, pin the chosen tab with `--tab id:<id>` using its `id` from `tabs --json`. On Safari, use `--tab <specific-url-substring>` because `<window>:<tab>` indexes can change. Reconfirm the target if the selector no longer matches; do not silently switch tabs.
- **Missing target:** For a login page, ask the user to open it in the correct browser and profile; do not use `open`. For a public page, use a mode that can create a tab in the selected browser (see `help`). Use `--foreground` only if bringing the browser to the front is intended; otherwise, if no suitable background mode exists, ask the user to open the page. For an existing tab that needs a screenshot, ask the user to bring it into view rather than opening another tab.

## 2. Check for verification

Run `info` on the selected tab before reading or interacting. If a human-verification page is visible or `info` reports `challenge: needs-human`, stop and tell the user. After they complete it, use `wait-challenge` to confirm it cleared. For `cloudflare-passive`, use `wait-challenge` directly. If it fails or times out, report it and stop. Never click, type into, read, or solve verification widgets.

## 3. Read or interact

- Start with `text` or `links` for reading and `snapshot` for finding controls. Use `html`, `screenshot`, or `eval` only as needed. Scope reads to the relevant content.
- Act on the pinned target and verify the result with `wait`, `text`, or `snapshot`. Run only code you wrote or trust, return only needed data, and never print or log cookies, tokens, or storage secrets.
- If successful synthetic input has no effect, try `--trusted` once where supported. Before sending real input, tell the user the browser will come to the front and ask them not to touch it. If input stops because the target changed, report it; do not retry automatically.
- Do not submit forms, post, purchase, delete, or change account settings unless the user asked. On command failure, stop and report it. You may follow a next step named in the error if it respects the login and verification rules above; report the result instead of rerunning the original command.

## 4. Close only what you created

Use `close --tab <target>` only for the same tab that your `open` reported as `created`, never for a `reused` or user-opened tab. If you cannot identify the original tab, leave it open. Closing a window's only tab removes the window in Chrome and can leave an empty window in Safari.
