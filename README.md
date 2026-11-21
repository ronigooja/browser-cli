# safari-cli

A dependency-free Bash tool for the user's real, logged-in macOS Safari through AppleScript.
The user logs in themselves; the tool stores no credentials or page markers.

## Setup

Requires macOS, Safari, and the system Bash/AppleScript tools.
Turn on Safari > Settings > Developer > **Allow JavaScript from Apple Events** once by hand
(requires Touch ID or a password). Leave it on; this tool never changes the setting
and never quits or restarts Safari. The first run may prompt macOS to let the calling
program control Safari; allow it.

Copy `safari-cli` to a directory on your PATH and make it executable.

## Commands

Syntax: `safari-cli <command> [options]`; `help` prints the reference, `version` the version.

| Command | Behavior |
| --- | --- |
| `status` | Probe `1+1` in an existing tab: print `on`, `off`, or `unknown` (no window/tab); never open a window. |
| `tabs` | List `<window>:<tab>`, title, URL separated by tabs. |
| `open <url>` | Focus a tab whose URL starts with the URL, otherwise open one; print its index. |
| `close [--tab T]` | Close the selected tab. |
| `eval [--tab T] <js>` | Evaluate an expression. |
| `file [--tab T] <path.js>` | Evaluate an expression read from a UTF-8 file. |
| `wait [--tab T] <js>` | Poll until truthy, printing nothing on success; report the last evaluation error on timeout. |

Exit codes: 0 success (`status`: on), 1 failure (including JavaScript errors,
lookup failures, timeout, or status off/unknown), 2 usage error.

## Options and results

- `--tab T`: 1-based `<window>:<tab>` or a URL substring (first match across windows);
  default is the front window's current tab. Indexes may change; prefer URL substrings.
- `--timeout N`: positive integer seconds for a command, default 30; wait polls share one deadline.
- `--`: end option parsing.

Strings are printed exactly, including trailing newlines; other values print JSON plus a
newline, and `undefined` prints an empty line. Shell command substitution strips trailing
newlines, so redirect to a file for exact comparisons.
Quotes, backslashes, and Unicode pass through a UTF-8 temporary file. Shell quoting still
applies. Wrap statements in an immediately invoked function; first/last-line comments work.
Evaluation is synchronous (promises are not awaited), in the page world and main frame only.

## Examples

```sh
safari-cli status
safari-cli open https://example.com/
safari-cli wait --timeout 20 --tab example.com 'document.querySelector("h1") !== null'
safari-cli eval --tab example.com '({url: location.href, title: document.title})'
safari-cli file --tab 1:2 ./inspect.js
```

## Security

Leaving the permission on lets any program allowed to control Safari run JavaScript in
logged-in pages. Review access in System Settings > Privacy & Security > Automation.
Run only trusted code; do not paste cookies or tokens into logs.
