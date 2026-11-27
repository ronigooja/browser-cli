"use strict";

const app = "com.ronigooja.agentbrowser.safariprobe";
const instance = crypto.randomUUID();
const idPattern = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;
const fixture = /^https:\/\/example\.com\/#safari-probe-(?:a|b|created)$/;
const destination = "https://example.com/#safari-probe-created";

const native = (phase, details = {}) => browser.runtime.sendNativeMessage(app, {phase, instance, ...details});

async function exchange(phase, details) {
    // Retry only lock contention, never a lost claim response or a browser operation.
    for (let attempt = 0; attempt < 12; ++attempt) {
        const response = await native(phase, details);
        if (response?.status !== "busy") return response;
        await new Promise(resolve => setTimeout(resolve, 25));
    }
    return {status: "mailbox_contended"};
}

browser.runtime.onMessage.addListener((message, sender, respond) => {
    if (message !== "probe-identity" || sender.id !== browser.runtime.id ||
        sender.url !== browser.runtime.getURL("identity.html")) return false;
    native("identify").then(respond, () => respond({status: "identity_unavailable"}));
    return true;
});

function fixtureMarker(tab) {
    return typeof tab.url === "string" && fixture.test(tab.url) ? tab.url.slice(tab.url.lastIndexOf("-") + 1) : null;
}

async function snapshot() {
    const windows = await browser.windows.getAll({populate: true});
    const regular = windows.filter(window => window.type === "normal" && window.incognito === false);
    if (regular.length > 8 || regular.reduce((count, window) => count + (window.tabs ?? []).length, 0) > 40) {
        return {status: "too_many_windows_or_tabs"};
    }
    return {
        status: "ok",
        windows: regular.map(window => ({
            id: window.id, focused: window.focused,
            tabs: (window.tabs ?? []).filter(tab => tab.incognito === false).map(tab => ({
                id: tab.id, active: tab.active, fixture: fixtureMarker(tab),
            })),
        })),
        otherWindowCount: windows.length - regular.length,
    };
}

// Serialized by scripting.executeScript. Return no content, URLs or credentials.
function inspectFixture(expected) {
    return location.href === expected && document.documentElement !== null;
}

function markerMatches(snapshot, marker) {
    return ["a", "b"].includes(marker) && !snapshot.windows.some(window =>
        window.tabs.some(tab => ["a", "b"].includes(tab.fixture) && tab.fixture !== marker));
}

async function inspect({tab: tabId, marker}, fresh) {
    const tab = fresh.windows.flatMap(window => window.tabs).find(tab => tab.id === tabId);
    if (!tab) return {status: "tab_not_in_profile_snapshot"};
    let permissionContains = null;
    try { permissionContains = await browser.permissions.contains({origins: ["https://example.com/*"]}); }
    catch { /* Diagnostic only. Safari controls injection authorization. */ }
    if (!tab.fixture) return {status: "fixture_url_hidden_or_outside_allowlist", permissionContains};
    if (tab.fixture !== marker || !markerMatches(fresh, marker)) {
        return {status: "fixture_profile_visibility_not_confirmed", permissionContains};
    }
    const expected = `https://example.com/#safari-probe-${marker}`;
    const inject = target => browser.scripting.executeScript({target, func: inspectFixture, args: [expected]});
    let first;
    try { [first] = await inject({tabId}); }
    catch { return {status: "injection_rejected", permissionContains}; }
    if (first?.frameId !== 0 || first.result !== true) {
        return {status: "fixture_not_read_in_top_frame", permissionContains};
    }
    let documentIdProbe = "not_returned";
    if (typeof first.documentId === "string" && first.documentId.length > 0) {
        try {
            const [confirmed] = await inject({tabId, documentIds: [first.documentId]});
            documentIdProbe = confirmed?.frameId === 0 && confirmed?.result === true &&
                confirmed?.documentId === first.documentId ? "matching_result_only" : "not_confirmed";
            if (documentIdProbe === "matching_result_only") {
                try {
                    const unrelated = await inject({tabId, documentIds: [crypto.randomUUID()]});
                    if (unrelated.length === 0) documentIdProbe = "unrelated_id_returned_no_results";
                    else documentIdProbe = "unrelated_id_also_injected";
                } catch {
                    documentIdProbe = "unrelated_id_rejected";
                }
            }
        } catch { documentIdProbe = "target_rejected"; }
    }
    return {status: "ok", permissionContains, documentIdProbe};
}

async function openFixture(grant, before) {
    const events = [];
    const note = event => { if (events.length < 32) events.push(event); };
    const onTabActivated = ({tabId, windowId}) => note({event: "tab_activated", tabId, windowId});
    const onWindowFocus = windowId => note({event: "window_focus_changed", windowId});
    browser.tabs.onActivated.addListener(onTabActivated);
    browser.windows.onFocusChanged.addListener(onWindowFocus);
    let attempted = false;
    try {
        if (Date.now() >= grant.expires * 1000) return {status: "expired_before_operation"};
        if (grant.operation === "add-tab") {
            const window = before.windows.find(window => window.id === grant.window);
            if (!window) {
                return {status: before.windows.length === 0 ? "no_regular_window" : "window_not_in_profile_snapshot", before};
            }
            if (!markerMatches(before, grant.marker) || !window.tabs.some(tab => tab.fixture === grant.marker)) {
                return {status: "fixture_profile_visibility_not_confirmed", before};
            }
            attempted = true;
            const tab = await browser.tabs.create({windowId: grant.window, url: destination, active: false});
            return {status: "created", id: tab.id, reportedWindow: tab.windowId, reportedActive: tab.active,
                before, after: await snapshot(), events};
        }
        if (before.windows.length !== 0 || before.otherWindowCount !== 0) {
            return {status: "not_zero_windows_in_extension_snapshot", before};
        }
        attempted = true;
        const window = await browser.windows.create({url: destination, type: "normal", focused: false, incognito: false});
        return {status: "created", id: window.id, reportedFocused: window.focused, before, after: await snapshot(), events};
    } catch {
        return {status: attempted ? "operation_outcome_unknown" : "operation_not_started", events};
    } finally {
        browser.tabs.onActivated.removeListener(onTabActivated);
        browser.windows.onFocusChanged.removeListener(onWindowFocus);
    }
}

async function run(grant) {
    const before = await snapshot();
    if (before.status !== "ok" || grant.operation === "windows") return before;
    if (grant.operation === "inspect") return inspect(grant, before);
    if (grant.operation === "add-tab" || grant.operation === "new-window") return openFixture(grant, before);
    return {status: "unknown_operation"};
}

function onDispatch(message) {
    // The payload is only a lookup key; neither shape can authorize an operation.
    const request = message?.request ?? message?.userInfo?.request;
    if (typeof request !== "string" || !idPattern.test(request)) return;
    (async () => {
        const grant = await exchange("claim", {request});
        if (grant?.status !== "claimed" || grant.instance?.toLowerCase() !== instance ||
            !idPattern.test(grant.profile) || !Number.isFinite(grant.expires) || Date.now() >= grant.expires * 1000) return;
        let result;
        try { result = await run(grant); }
        catch { result = {status: "probe_api_failed"}; }
        await exchange("result", {request, result});
    })().catch(() => { /* The host reports a bounded timeout, not a successful delivery. */ });
}

const port = browser.runtime.connectNative(app);
port.onMessage.addListener(onDispatch);
