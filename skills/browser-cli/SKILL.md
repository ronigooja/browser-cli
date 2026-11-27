---
name: browser-cli
description: Drives the user's real, logged-in macOS Safari or Chrome through the `browser-cli` Bash tool. Use for reading pages behind a login, running JavaScript in a tab, listing/opening/closing tabs, or scraping sites where fetch tools fail because of login or anti-bot checks.
---

# browser-cli

Use `browser-cli` beside this skill when installed, or at the repository root (`../../browser-cli` from this directory) in a source checkout. Use its absolute path for every call, and run `help` for syntax and output.

## 1. Select a browser and tab

- If the task does not explicitly require the user's login, start by trying to read the target as public content. Do not infer a login requirement from the site name, a blank page, a timeout, or a verification challenge. Switch to the login workflow only if the requested content is unavailable and access to it explicitly requires sign-in.
- If the task needs the user's login, ask which browser holds it unless the user already said. Otherwise, if you need the user to choose a browser, ask only which browser to use; do not ask for a signed-in session.
- Start with `--browser <browser> status`. On `off`, report the permission diagnostic and ask the user to enable the setting. Do not change it or restart the browser. On `unknown`, use the missing-target rule below, then check `status` again once the page is open.
- If a tab command fails with `-1728`, check `tabs`. If there are no tabs, follow the `unknown` recovery above. If tabs exist or the check fails, report the original error and stop. Do not retry the failed tab command automatically.
- Identify the tab with `tabs`. If the task needs the user's Chrome login, confirm the candidate's title and URL with the user: `tabs --json` does not show profiles. On Chrome, pin the tab with `--tab id:<id>` using the ID from `tabs --json` or `open --background`. Safari has no stable tab ID: before acting, check that the URL substring matches just the intended tab. Stop if the match is missing or ambiguous; do not silently switch tabs.
- **Missing target:** If the task needs the user's login, ask them to open the target in the correct browser and profile; do not use `open`. Otherwise, use `open --background` when available (see `help`). If background opening is unavailable, ask the user to open the page unless bringing the browser to the front is intended. Do not ask them to sign in just to open the target. Do not run `install-background` without approval or retry a failed background open in the foreground. If a screenshot needs an existing tab to be visible, ask the user to bring it into view.

## 2. Check for verification

Run `info` on the selected tab before reading or interacting. For `cloudflare-passive`, use `wait-challenge`. If human verification is visible or reported by either command, stop and tell the user. After they complete it, use `wait-challenge` to check again. `challenge: none` means no known challenge was detected; it does not override a visible verification page. Report any other failure and stop. Never click, type into, read, or solve verification widgets.

## 3. Read or interact

- Start with `text` or `links` for reading and `snapshot` for finding controls. Use `html`, `screenshot`, or `eval` only as needed. Scope reads to the relevant content.
- Act on the pinned target and verify the result with `wait`, `text`, or `snapshot`. Run only code you wrote or trust, return only needed data, and never print or log cookies, tokens, or storage secrets.
- If successful synthetic input has no effect, try `--trusted` once where supported. Before sending real input, tell the user the browser will come to the front and ask them not to touch it. If input stops because the target changed, report it; do not retry automatically.
- Do not submit forms, post, purchase, delete, or change account settings unless the user asked. On command failure, stop and report it. You may follow a next step named in the error if it respects the login and verification rules above; report the result instead of rerunning the original command.

## 4. Close only what you created

Use `close --tab <target>` only for the same tab that your `open` reported as `created`, never for a `reused` or user-opened tab. If you cannot identify the original tab, leave it open. Closing a window's only tab removes the window in Chrome and can leave an empty window in Safari.
