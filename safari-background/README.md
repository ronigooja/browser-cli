# Agent Browser Safari background candidate

This separate signed app/extension implements the modern background read protocol on macOS Safari. It does not replace the restricted `safari-prototype` app. The candidate needs real Safari acceptance and focus tests before deployment; it does not use Apple Events, host dispatch, browser launch, network servers, content-page command entry, or a fallback read/open path.

## Build and package

On a GitHub-hosted macOS runner with Xcode 16+:

```sh
xcodebuild -project safari-background/AgentBrowserSafari.xcodeproj -scheme AgentBrowserSafari \
  -configuration Release -destination 'platform=macOS' \
  -derivedDataPath "$RUNNER_TEMP/agent-browser-safari" CODE_SIGNING_ALLOWED=NO build
app="$RUNNER_TEMP/agent-browser-safari/Build/Products/Release/AgentBrowserSafari.app"
bundle="$app/Contents/PlugIns/AgentBrowserSafariExtension.appex/Contents/Resources"
cmp extension/shared/read-page.js "$bundle/read-page.js"
test -f "$bundle/manifest.json"
"$app/Contents/MacOS/AgentBrowserSafari" verify-gate
node --test safari-background/background.test.js  # if Node.js is available
```

For live testing use a runner provisioned for signing both targets with the same Apple Development team: omit `CODE_SIGNING_ALLOWED=NO` and pass `DEVELOPMENT_TEAM=<team-id>`. Verify `codesign -d --entitlements :-` on the app and its `.appex`: both must be sandboxed and share `<team-id>.com.ronigooja.agentbrowser.safaribackground`. Verify the expanded group in both `Info.plist` files. Unsigned artifacts and unsigned-extension developer settings are not substitutes for the signed App Group. Registration and distribution/notarization are separate from compile/packaging checks.

## Explicit setup

1. Place the signed app at `~/Applications/AgentBrowserSafari.app` (or configure its absolute binary path) and open that **app** once without arguments to register its embedded extension. This does not open Safari. Enable the extension only in the intended **named** Safari profile; turn off **Allow in Private Browsing**. Do not use a Safari web app or the unnamed default context. The operator, not a UUID or window order, identifies the named profile in Safari.
2. In that profile, use the **Agent Browser Safari Setup** popup. It must show a browser-supplied UUID. Click **Grant all HTTP(S) access and enable** and approve the single wildcard grant (`http://*/*` and `https://*/*`), not an individual website or one-time current-site permission. If Safari cannot grant both scopes from this setup flow, do not work around it with per-site prompts; the adapter is unavailable until the one-time grant is confirmed by `permissions.contains`. Set the MCP process environment:

   ```sh
   export AGENT_BROWSER_SAFARI_PROFILE='<UUID shown by the setup popup>'
   # Only for a non-default installation:
   export AGENT_BROWSER_SAFARI_BINARY='/absolute/path/AgentBrowserSafari.app/Contents/MacOS/AgentBrowserSafari'
   ```

3. The popup may remain closed for all calls. **Disable background requests** clears the local profile selection and stops polling; revoke permissions or disable the extension in Safari Settings to remove browser access as well. The JavaScript selection is checked against `SFExtensionProfileKey` in native code on every pull/result. Missing, invalid, private, different-profile and stale-instance contexts do not get write authority.

## Internal adapter contract

Load `safari-background/bridge.py` and call `request(operation, payload, timeout)` like the Chrome adapter. Timeouts are seconds, >0 and ≤300. The subprocess command is `AgentBrowserSafari ipc`; it receives one UTF-8 JSON object followed by EOF and returns one UTF-8 JSON object. Input `{v:1,id,profile,operation,payload,expires}` has a 32-character lowercase hex request ID, explicit profile UUID, and absolute Unix milliseconds deadline. Output is `{v:1,id,status,...}`; the Python adapter strips version and request ID. Neither the binary nor the Python adapter is a public user CLI.

- `status {}` checks transport, private access, and the broad grant. It does not establish document pinning or prove that a window exists.
- `find {url}` returns `tabs:[{ref,url,title}]` for origin/path matches in eligible regular windows. `open {url}` reuses exactly one exact match, or creates an inactive tab in a single unambiguous regular window and returns `{tab,created,ownership?}`. It never creates a window. No eligible regular window returns `no_regular_window`, not a claim of global zero windows.
- `read {ref,expected_url,selector?,wait_for_selector?,include_links?}` uses the packaged `extension/shared/read-page.js` with one top-frame document UUID and exact canonical URL. Before extracting any content in an instance, it checks that an unrelated UUID cannot execute a no-data marker and that a matching UUID can. For every extraction it requires the matching UUID and frame in the result and rechecks the URL, document UUID and navigation generation before release. No frame-only fallback. Safari does not report Chromium's `documentLifecycle`; unsupported or inconclusive evidence returns `read_unavailable` or `target_changed`. Human-verification and login states are returned as statuses, never solved by the extension.
- `close {ref,ownership}` accepts only an inactive, still-owned, originally created tab from this background instance and window. Selection or activation can relinquish ownership. Cleanup consumes the token once; do not automatically repeat `open_unconfirmed` or `cleanup_unconfirmed`. Mutations that visibly activate Safari during the host's observation window are reported unconfirmed. Absence of an activation event is not proof of no transient UI change.

The subprocess does not launch Safari and reports `profile_required`, `extension_required`, `disconnected`, `busy`, `timeout`, or operation-specific errors instead of guessing. Request/result JSON is capped at 64 KiB; native state has bounded size and one lease per profile. A UID-owned private App Group directory, non-symlink file opens, atomic replacement, locks, expiry and single claim/result protect the local protocol. It is not a defense against the same OS user, a compromised extension, or an administrator. A killed requester can leave data in the private mailbox until a later poll/new lease; the handler rejects expired work and never replays an ambiguous mutation. The live instance nonce and ownership refs exist only in memory. No user data is logged by the native handler.

## Live acceptance

Use a disposable named profile and check the default/no-UUID context separately. On a consented workstation after the signed hosted build, keep a regular Safari window open, leave Terminal frontmost, and from the checkout root run:

```sh
PYTHONPATH=safari-background python3 - <<'PY'
from bridge import request

url = "https://example.com/"
print(request("status", {}, 4))
print(request("find", {"url": url}, 4))
PY
```

For a separately consented new-tab test, use an HTTP(S) URL not already open. Require `created: true`, unchanged selection and foreground, use the returned `ref` and exact `url` in `read`, and pass the returned `ownership` only to `close`. Repeat status/find with Safari idle, then with all windows closed without quitting. A stopped browser must remain stopped. Test two named profiles for isolation, the default scenario for missing metadata, grant revocation and per-site Ask/Deny overrides for absence of prompts, an actual navigation/redirect during read, ref invalidation after restart, and owned close without foreground or selection changes. Safari can generate permission UI internally in some tab/scripting APIs; this build requests no per-site permissions, gates operations on broad access and will not extract when the browser withholds the frame URL. The target release's UI and permission-change races must still be checked live. Do not deploy on a release/configuration that presents site prompts during a request.

Polling is one second after each complete native exchange and requires the browser-managed MV2 persistent background to be alive. This avoids a Safari launch or slow per-operation alarm; it neither wakes a dead context nor promises low latency when Safari throttles. [MDN MV2 persistence data](https://github.com/mdn/browser-compat-data/blob/main/webextensions/manifest/background.json) and [Apple background guidance](https://developer.apple.com/documentation/safariservices/optimizing-your-web-extension-for-safari) identify it as a macOS Safari candidate, not proof of current acceptance. [WebNavigation](https://github.com/mdn/browser-compat-data/blob/main/webextensions/api/webNavigation.json) and [scripting](https://github.com/mdn/browser-compat-data/blob/main/webextensions/api/scripting.json) list relevant document IDs in Safari 18.4+. MV2 + scripting, signed App Group access, the all-sites grant, inactive profile selection, document targeting and background focus behavior need signed live verification. No automatic wake or MV3 fallback is attempted.
