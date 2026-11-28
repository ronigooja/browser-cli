# Agent Browser for Safari

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
```

On Linux, run `node --test safari-background/background.test.js` and `python3 -m unittest discover -s safari-background -p bridge_test.py` for the mocked adapter checks.

For live testing use a runner provisioned for signing both targets with the same Apple Development team: omit `CODE_SIGNING_ALLOWED=NO` and pass `DEVELOPMENT_TEAM=<team-id>`. Verify `codesign -d --entitlements :-` on the app and its `.appex`: both must be sandboxed and share `<team-id>.com.ronigooja.agentbrowser.safaribackground`. Verify the expanded group in both `Info.plist` files. Unsigned artifacts and unsigned-extension developer settings are not substitutes for the signed App Group. Registration and distribution/notarization are separate from compile/packaging checks.

## Explicit setup

1. Place the signed app at `~/Applications/AgentBrowserSafari.app` (or configure its absolute binary path) and open that **app** once without arguments to register its embedded extension. This does not open Safari. Enable the extension only in the intended **named** Safari profile; turn off **Allow in Private Browsing**. Do not use a Safari web app or the unnamed default context. The operator, not a UUID or window order, identifies the named profile in Safari.
2. In that profile, open an ordinary HTTP(S) page such as `https://example.com/`, not the Safari Start Page. Use the extension’s toolbar button to grant access to **all websites** once. In the **Agent Browser Safari Setup** popup, click **Enable background requests**. Expect `enabled` and a UUID. If the all-sites option is unavailable, do not grant access site by site. Set the MCP process environment:

   ```sh
   export AGENT_BROWSER_SAFARI_PROFILE='<UUID shown by the setup popup>'
   # Only for a non-default installation:
   export AGENT_BROWSER_SAFARI_BINARY='/absolute/path/AgentBrowserSafari.app/Contents/MacOS/AgentBrowserSafari'
   ```

3. The popup may remain closed for all calls. **Disable background requests** clears the local profile selection and stops polling; revoke permissions or disable the extension in Safari Settings to remove browser access as well. The JavaScript selection is checked against `SFExtensionProfileKey` in native code on every pull/result. Missing, invalid, private, different-profile and stale-instance contexts do not get write authority.

For an explicitly approved update, replace the existing signed app with a build that contains the matching extension; do not install a second copy. Recheck the intended profile's settings, popup and connection. A valid v1 host rejection signals `update_required`; ordinary disconnection does not prove an update or reinstall is needed. v1 and v2 use separate mailboxes, and neither can claim the other's leases.

## Removal assistance

Use Safari Settings → Extensions to check which signed Agent Browser extension is enabled and in which named profiles. If removal is desired, the user can disable background requests in the setup popup, turn off the extension for each enabled profile, remove the signed host app using Safari's instructions, and clear `AGENT_BROWSER_SAFARI_PROFILE` / `AGENT_BROWSER_SAFARI_BINARY` from their MCP environment as appropriate. A disconnected status cannot prove removal. The adapter does not disable extensions, delete apps or App Group data, or infer ownership of paths from process environment or profile UUIDs.

## Internal adapter contract

Load `safari-background/bridge.py` and call `request(operation, payload, timeout)` like the Chrome adapter. Timeouts are seconds, >0 and ≤300. The subprocess command is `AgentBrowserSafari ipc`; it receives one UTF-8 JSON object followed by EOF and returns one UTF-8 JSON object. Input `{v:2,id,profile,operation,payload,expires}` has a 32-character lowercase hex request ID, explicit profile UUID, and absolute Unix milliseconds deadline. Output is `{v:2,id,status,...}`; the Python adapter strips version and request ID. Neither the binary nor the Python adapter is a public user CLI.

- `status {}` checks transport, private access, and the broad grant. It does not establish document pinning or prove that a window exists.
- `find {url}` returns `tabs:[{ref,url,title}]` for origin/path matches in eligible regular windows. `open {url}` reuses exactly one exact match, or creates an inactive tab in a single unambiguous regular window and returns `{tab,created,ownership?}`. It never creates a window. No eligible regular window returns `no_regular_window`, not a claim of global zero windows.
- `list {}` returns up to 32 `{ref,url,title,active,window_id}` rows for HTTP(S) tabs in the authorized regular profile. `truncated:true` indicates inventory results omitted due to count, reference, or transport bounds.
- `read {ref,expected_url,selector?,wait_for_selector?,include_links?}` uses the packaged `extension/shared/read-page.js` with one top-frame document UUID and exact canonical URL. Before extracting any content in an instance, it checks that an unrelated UUID cannot execute a no-data marker and that a matching UUID can. For every extraction it requires the matching UUID and frame in the result and rechecks the URL, document UUID and navigation generation before release. No frame-only fallback. Safari does not report Chromium's `documentLifecycle`; unsupported or inconclusive evidence returns `read_unavailable` or `target_changed`. Human-verification and login states are returned as statuses, never solved by the extension.
- `page {ref,expected_url,kind,...}` injects the packaged `agentBrowserPageOperation` into the same verified isolated top document. Only fixed operations and their bounded arguments are accepted. There is no arbitrary JavaScript or trusted keyboard input. After dispatch, a possible mutation without consistent postchecks returns `action_unconfirmed` rather than reporting definite failure.
- `tab {ref,expected_url,kind,url?}` supports `navigate`, `activate`, and explicit `close`, all distinct from ownership-token cleanup. Tab writes relinquish ownership. Navigation sends a URL-only update and requires an exact committed destination and unchanged tab selection/window focus; it does not expose redirected content. Activation and close are explicit, not fallbacks. Safari does not offer an atomic compare-and-update tab API, so these operations check the current document as close to dispatch as possible and verify the available postconditions.
- `front_open {url}` explicitly creates and focuses an active tab in one eligible existing window and returns `{tab,created}` without ownership. It never launches Safari or creates a window.
- `capture {ref,expected_url,selector?}` requires an already-active tab in a focused, regular window, plus stable selection and document checks and the shared page gate before and after capture. It returns `image` as an `image/png` data URL only if it fits the 64 KiB transport; otherwise it returns `response_too_large`. Selector capture and fallback activation/cropping are not supported.
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

The MV2 background polls one second after each native exchange. A suspended context cannot be woken by the adapter; no MV3 fallback is attempted. Document targeting requires the [Safari 18.4+ scripting APIs](https://github.com/mdn/browser-compat-data/blob/main/webextensions/api/scripting.json).
