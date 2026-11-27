"use strict";

const app = "com.ronigooja.agentbrowser.safariprobe";
const instance = crypto.randomUUID();
const backgroundStartedAt = Date.now() / 1000;
const pullAlarm = "safari-probe-pull-once";
const warmIntervalMs = 1000;
const warmLifetimeMs = 120000;
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

function fixtureMarker(tab) {
    return typeof tab.url === "string" && fixture.test(tab.url) ? tab.url.slice(tab.url.lastIndexOf("-") + 1) : null;
}

async function snapshot({includeFixtures = true} = {}) {
    const windows = await browser.windows.getAll({populate: true});
    const regular = windows.filter(window => window.type === "normal" && window.incognito === false);
    if (regular.length > 8 || regular.reduce((count, window) => count + (window.tabs ?? []).length, 0) > 40) {
        return {status: "too_many_windows_or_tabs"};
    }
    return {
        status: "ok",
        windows: regular.map(window => ({
            id: window.id, focused: window.focused,
            tabs: (window.tabs ?? []).filter(tab => tab.incognito === false).map(tab => includeFixtures ? {
                id: tab.id, active: tab.active, fixture: fixtureMarker(tab),
            } : {id: tab.id, active: tab.active}),
        })),
        otherWindowCount: windows.length - regular.length,
        windowClassification: {
            returned: windows.length,
            normalMissingIncognitoFlag: windows.filter(window => window.type === "normal" &&
                typeof window.incognito !== "boolean").length,
            missingType: windows.filter(window => typeof window.type !== "string").length,
            regularWithoutTabArray: regular.filter(window => !Array.isArray(window.tabs)).length,
            regularTabsMissingIncognitoFlag: regular.reduce((count, window) => count +
                (window.tabs ?? []).filter(tab => typeof tab.incognito !== "boolean").length, 0),
        },
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

function validGrant(grant) {
    return grant?.status === "claimed" && typeof grant.instance === "string" && grant.instance.toLowerCase() === instance &&
        typeof grant.profile === "string" && idPattern.test(grant.profile) &&
        Number.isFinite(grant.expires) && Date.now() < grant.expires * 1000;
}

function onDispatch(message) {
    // The payload is only a lookup key; neither shape can authorize an operation.
    const request = message?.request ?? message?.userInfo?.request;
    if (typeof request !== "string" || !idPattern.test(request)) return;
    (async () => {
        const grant = await exchange("claim", {request});
        if (!validGrant(grant)) return;
        let result;
        try { result = await run(grant); }
        catch { result = {status: "probe_api_failed"}; }
        await exchange("result", {request, result});
    })().catch(() => { /* The host reports a bounded timeout, not a successful delivery. */ });
}

let port;
function startPush() {
    if (port) return;
    const connected = browser.runtime.connectNative(app);
    port = connected;
    connected.onMessage.addListener(onDispatch);
    connected.onDisconnect.addListener(() => { if (port === connected) port = undefined; });
}

function stopPush() {
    if (!port) return;
    port.onMessage.removeListener(onDispatch);
    port.disconnect();
    port = undefined;
}

// Register synchronously so an alarm can wake this nonpersistent background script. The
// persisted mode prevents a native port opening on restart, including on an alarm wake.
let pullMode = false;
const alarms = browser.alarms;
async function pullOnce(wake, alarmScheduledAt) {
    const evidence = {wake, backgroundStartedAt};
    if (Number.isFinite(alarmScheduledAt)) evidence.alarmScheduledAt = alarmScheduledAt;
    const grant = await native("pull", evidence);
    if (!validGrant(grant) || grant.operation !== "windows" ||
        typeof grant.request !== "string" || !idPattern.test(grant.request)) return;
    let result;
    try { result = await snapshot({includeFixtures: false}); }
    catch { result = {status: "probe_api_failed"}; }
    await native("pull-result", {request: grant.request, result});
}
alarms?.onAlarm?.addListener(alarm => {
    if (alarm.name !== pullAlarm) return;
    modeReady.then(() => {
        if (pullMode) return pullOnce("alarm", alarm.scheduledTime / 1000);
    }).catch(() => { /* No automatic rearm or push fallback. */ });
});

const modeReady = browser.storage.local.get("pullMode").then(value => {
    pullMode = value.pullMode === true;
    if (!pullMode) {
        try { startPush(); }
        catch { /* A push startup failure must not prevent the operator from arming pull. */ }
    }
});
// Don't open the push port if the persistent mode could not be read.
modeReady.catch(() => {});

const hasAlarms = typeof alarms?.create === "function" && typeof alarms?.get === "function" &&
    typeof alarms?.clear === "function" && typeof alarms?.onAlarm?.addListener === "function";

// A separate, explicitly armed warm-context test. State lives only in this context;
// only the browser-owned alarm may wake a restarted background page.
let warmSession;
function stopWarmPoll() {
    if (warmSession) clearTimeout(warmSession.timer);
    warmSession = undefined;
}

function startWarmPoll() {
    const session = {ends: performance.now() + warmLifetimeMs, ticks: 0};
    warmSession = session;
    const tick = async () => {
        if (warmSession !== session) return;
        if (!pullMode || performance.now() >= session.ends || session.ticks >= warmLifetimeMs / warmIntervalMs) {
            stopWarmPoll();
            return;
        }
        session.ticks += 1;
        try { await pullOnce("warm_poll"); }
        catch { /* Continue observing; a lost claim is never replayed. */ }
        if (warmSession === session) session.timer = setTimeout(tick, warmIntervalMs);
    };
    session.timer = setTimeout(tick, warmIntervalMs);
}

async function control(message) {
    await modeReady;
    if (message === "probe-mode") return {status: "mode", mode: pullMode ? "pull" : "push", hasAlarms, pushConnected: !!port};
    if (message === "probe-use-push") {
        if (hasAlarms) await alarms.clear(pullAlarm);
        stopWarmPoll();
        await browser.storage.local.remove("pullMode");
        pullMode = false;
        startPush();
        return {status: "push_enabled"};
    }
    if (!hasAlarms) return {status: "alarms_unavailable"};
    stopWarmPoll();
    await browser.storage.local.set({pullMode: true});
    pullMode = true;
    stopPush();
    // A one-shot 60s delay leaves room for a cold background wake on WebKit and
    // avoids newer (shorter) clamp assumptions. Never rearm automatically.
    await alarms.create(pullAlarm, {delayInMinutes: 1});
    const scheduled = await alarms.get(pullAlarm);
    if (!scheduled || !Number.isFinite(scheduled.scheduledTime)) return {status: "alarm_not_confirmed"};
    const warm = message === "probe-arm-warm";
    if (warm) startWarmPoll();
    return {status: warm ? "warm_pull_armed" : "pull_armed", scheduledTime: scheduled.scheduledTime};
}

let controls = Promise.resolve();
browser.runtime.onMessage.addListener((message, sender, respond) => {
    if (sender.id !== browser.runtime.id || sender.url !== browser.runtime.getURL("identity.html")) return false;
    if (message === "probe-identity") {
        native("identify").then(respond, () => respond({status: "identity_unavailable"}));
        return true;
    }
    if (!["probe-arm-pull", "probe-arm-warm", "probe-use-push", "probe-mode"].includes(message)) return false;
    controls = controls.then(() => control(message)).then(respond, () => respond({status: "mode_control_failed"}));
    return true;
});
