"use strict";

const app = "com.ronigooja.agentbrowser.safaribackground";
const instance = crypto.randomUUID();
const origins = ["http://*/*", "https://*/*"];
const uuid = /^[\da-f]{8}(?:-[\da-f]{4}){3}-[\da-f]{12}$/i;
const requestID = /^[\da-f]{32}$/;
const limit = 65536;
const refs = new Map(), tabs = new Map();
const interval = 1000;
let enabledProfile;
let timer;
let polling = false;
let selectionEpoch = 0;
let lastActivation = 0;
let readPinQualified = false;

const native = (phase, extra = {}) => browser.runtime.sendNativeMessage(app, {v: 1, phase, instance, ...extra});
const expired = expires => Date.now() >= expires;
const isID = id => Number.isSafeInteger(id) && id >= 0;
const regular = tab => tab?.incognito === false && isID(tab.id) && isID(tab.windowId);
const normal = window => window?.type === "normal" && window.incognito === false && isID(window.id);

function httpURL(value) {
  if (typeof value !== "string" || value.length > 4096 || !/^https?:\/\//i.test(value) || /[\\\s\x00-\x1f\x7f]/u.test(value)) return null;
  if (/^https?:\/\/[^/?#]*@/i.test(value)) return null;
  try {
    const url = new URL(value);
    return url.username || url.password || url.href.length > 4096 ? null : url;
  } catch (_) { return null; }
}

function validPayload(operation, data) {
  if (!data || typeof data !== "object" || Array.isArray(data)) return false;
  const only = (...keys) => Object.keys(data).every(key => keys.includes(key));
  const ref = value => typeof value === "string" && value.length === 73 && value.split(".").every(part => uuid.test(part));
  const selector = value => typeof value === "string" && value.length > 0 && value.length <= 2000;
  if (operation === "status") return only();
  if (operation === "find" || operation === "open") return only("url") && !!httpURL(data.url);
  if (operation === "close") return only("ref", "ownership") && ref(data.ref) && typeof data.ownership === "string" && uuid.test(data.ownership);
  return operation === "read" && only("ref", "expected_url", "selector", "wait_for_selector", "include_links") &&
    ref(data.ref) && !!httpURL(data.expected_url) &&
    (!("selector" in data) || selector(data.selector)) && (!("wait_for_selector" in data) || selector(data.wait_for_selector)) &&
    (!("include_links" in data) || typeof data.include_links === "boolean");
}

async function setupStatus() {
  if (!enabledProfile) return "disconnected";
  return permissionsStatus();
}

async function permissionsStatus() {
  // Manifest incognito:not_allowed is not supported in Safari. Demand a positive runtime answer.
  if (browser.extension?.inIncognitoContext !== false ||
      await browser.extension?.isAllowedIncognitoAccess?.() !== false) return "private_access_enabled_or_unverified";
  if (await browser.permissions?.contains?.({origins}) !== true) return "site_access_required";
  return "ok";
}

function forget(id) {
  const entry = tabs.get(id);
  if (entry) refs.delete(entry.ref);
  tabs.delete(id);
}

const removed = browser.tabs?.onRemoved;
const activated = browser.tabs?.onActivated;
const windowFocusChanged = browser.windows?.onFocusChanged;
const beforeNavigate = browser.webNavigation?.onBeforeNavigate;
const committed = browser.webNavigation?.onCommitted;
removed?.addListener(forget);
activated?.addListener(({tabId}) => {
  lastActivation++;
  const entry = tabs.get(tabId);
  if (entry) delete entry.ownership; // The user may have adopted this tab.
});
windowFocusChanged?.addListener(() => { lastActivation++; });
browser.tabs?.onReplaced?.addListener((_new, old) => forget(old));
for (const name of ["onBeforeNavigate", "onCommitted", "onHistoryStateUpdated", "onReferenceFragmentUpdated"]) {
  browser.webNavigation?.[name]?.addListener(({tabId, frameId}) => {
    const entry = tabs.get(tabId);
    if (frameId === 0 && entry) entry.navigation++;
  });
}
browser.tabs?.onUpdated?.addListener((tabId, changes) => {
  const entry = tabs.get(tabId);
  if (entry && (changes.url !== undefined || changes.status === "loading")) entry.navigation++;
});

function describe(tab, url) {
  let entry = tabs.get(tab.id);
  if (entry && entry.window !== tab.windowId) { forget(tab.id); entry = undefined; }
  if (!entry) {
    if (tabs.size >= 256) return null;
    entry = {id: tab.id, window: tab.windowId, ref: `${instance}.${crypto.randomUUID()}`, navigation: 0};
    tabs.set(tab.id, entry);
    refs.set(entry.ref, entry);
  }
  return {ref: entry.ref, url: url.href, title: typeof tab.title === "string" ? tab.title.slice(0, 500) : ""};
}

async function inventory() {
  const windows = await browser.windows.getAll({populate: true});
  if (!Array.isArray(windows) || windows.length > 32) return null;
  const eligible = windows.filter(normal);
  if (eligible.some(window => !Array.isArray(window.tabs)) ||
      eligible.reduce((n, window) => n + window.tabs.length, 0) > 256) return null;
  return eligible;
}

async function currentTab(entry) {
  if (refs.get(entry.ref) !== entry) return null;
  // Check the known window and current tab in the same snapshot.
  const window = await browser.windows.get(entry.window, {populate: true}).catch(() => null);
  if (refs.get(entry.ref) !== entry || !normal(window) || window.id !== entry.window || !Array.isArray(window.tabs)) return null;
  return window.tabs.find(tab => regular(tab) && tab.id === entry.id && tab.windowId === entry.window) ?? null;
}

const observed = tab => httpURL(tab?.pendingUrl ?? tab?.url);
const changed = url => ({status: "target_changed", ...(url ? {final_url: url.href} : {})});
function boundary(tab, expected) {
  const url = observed(tab);
  if (url?.href !== expected) return changed(url);
  if (tab.pendingUrl !== undefined || tab.discarded === true || tab.status === "loading" || httpURL(tab.url)?.href !== expected) {
    return {status: "page_loading", final_url: expected};
  }
  return null;
}

async function topDocument(id) {
  try { return await browser.webNavigation?.getFrame?.({tabId: id, frameId: 0}); }
  catch (_) { return null; }
}

function trustedDocument(frame, expected) {
  // Safari reports documentId, but not documentLifecycle. Demand an error-free top frame,
  // exact URL and an actual UUID, then verify navigation and the result's document ID.
  return frame?.url === expected && frame.parentFrameId === -1 && frame.errorOccurred === false &&
    typeof frame.documentId === "string" && uuid.test(frame.documentId);
}

function noDataMarker() { return true; }

const readStatuses = new Set(["ok", "verification_required", "passive_challenge", "login_required", "page_loading",
  "content_not_ready", "invalid_wait_selector", "invalid_selector", "no_match", "no_content"]);

async function read({ref, expected_url, selector, wait_for_selector, include_links}, expires) {
  const selection = selectionEpoch;
  const expected = httpURL(expected_url).href;
  const unavailable = {status: "read_unavailable", final_url: expected};
  if (!browser.scripting?.executeScript || !beforeNavigate?.addListener || !committed?.addListener || !removed?.addListener) return unavailable;
  const entry = refs.get(ref);
  if (!entry) return changed();
  const navigation = entry.navigation;
  const mismatch = boundary(await currentTab(entry), expected);
  if (mismatch) return mismatch;
  const frame = await topDocument(entry.id);
  if (frame?.url && frame.url !== expected) return changed(httpURL(frame.url));
  if (!trustedDocument(frame, expected)) return unavailable;
  const target = httpURL(expected);
  if (await browser.permissions.contains({origins: [`${target.protocol}//${target.hostname}/*`]}) !== true) return unavailable;
  if (expired(expires)) return {status: "timeout"};

  // Qualify this background instance before its first extraction. Every extraction
  // still requires a matching injection result and both pre/post document checks.
  const script = (documentId, func, args) => browser.scripting.executeScript({
    target: {tabId: entry.id, documentIds: [documentId]}, world: "ISOLATED", func, ...(args ? {args} : {}),
  });
  if (!readPinQualified) {
    const negative = await script(crypto.randomUUID(), noDataMarker).catch(() => null);
    if (negative !== null && (!Array.isArray(negative) || negative.length !== 0)) return unavailable;
    const positive = await script(frame.documentId, noDataMarker).catch(() => null);
    if (selection !== selectionEpoch || !Array.isArray(positive) || positive.length !== 1 || positive[0].documentId !== frame.documentId ||
        positive[0].frameId !== 0 || positive[0].result !== true) return unavailable;
    readPinQualified = true;
  }
  if (await setupStatus() !== "ok") return unavailable;
  const pin = await topDocument(entry.id);
  if (refs.get(ref) !== entry || entry.navigation !== navigation || !trustedDocument(pin, expected) ||
      pin.documentId !== frame.documentId) return changed();
  if (expired(expires)) return {status: "timeout"};

  const results = await script(frame.documentId, readPage, [{expectedURL: expected,
    ...(selector !== undefined ? {selector} : {}), ...(wait_for_selector !== undefined ? {waitForSelector: wait_for_selector} : {}),
    includeLinks: include_links === true}]).catch(() => null);
  const postMismatch = boundary(await currentTab(entry), expected);
  if (postMismatch) return postMismatch;
  const post = await topDocument(entry.id);
  if (refs.get(ref) !== entry || entry.navigation !== navigation || !trustedDocument(post, expected) ||
      post.documentId !== frame.documentId) return changed(httpURL(post?.url));
  if (expired(expires)) return {status: "timeout"};
  if (await setupStatus() !== "ok" || refs.get(ref) !== entry || entry.navigation !== navigation || expired(expires)) return unavailable;
  if (!Array.isArray(results) || results.length !== 1 || results[0].documentId !== frame.documentId || results[0].frameId !== 0) return unavailable;
  const result = results[0].result;
  if (!readStatuses.has(result?.status) || (result.status === "ok" && result.final_url !== expected)) return changed();
  return {...result, final_url: expected};
}

async function findOpen(operation, {url}, expires) {
  const selection = selectionEpoch;
  const target = httpURL(url);
  const windows = await inventory();
  if (!windows) return {status: "window_snapshot_unavailable"};
  if (selection !== selectionEpoch) return {status: "disconnected"};
  if (expired(expires)) return {status: "timeout"};
  const found = [];
  let unknownTab = false;
  for (const window of windows) {
    for (const tab of window.tabs) {
      if (!regular(tab) || tab.windowId !== window.id) { unknownTab = true; continue; }
      if (typeof (tab.pendingUrl ?? tab.url) !== "string" || !(tab.pendingUrl ?? tab.url)) unknownTab = true;
      const actual = observed(tab);
      if (!actual || (operation === "find" ? actual.origin !== target.origin || actual.pathname !== target.pathname : actual.href !== target.href)) continue;
      const description = describe(tab, actual);
      if (!description) return {status: "response_too_large"};
      found.push(description);
    }
  }
  if (operation === "find") return {status: "ok", tabs: found};
  if (found.length > 1) return {status: "tab_ambiguous", tabs: found};
  if (found.length === 1) return {status: "ok", tab: found[0], created: false};
  // The accessible profile snapshot cannot establish global zero windows. Never create a window.
  if (windows.length === 0) return {status: "no_regular_window"};
  if (unknownTab) return {status: "window_snapshot_unavailable"};
  // Avoid guessing where to write if several otherwise eligible windows are present.
  const focused = windows.filter(window => window.focused === true);
  const window = windows.length === 1 ? windows[0] : focused.length === 1 ? focused[0] : null;
  if (!window || typeof window.focused !== "boolean" || !removed?.addListener || !activated?.addListener ||
      !windowFocusChanged?.addListener || tabs.size >= 256) {
    return {status: "window_snapshot_unavailable"};
  }
  const selected = window.tabs.filter(tab => regular(tab) && tab.windowId === window.id && tab.active === true);
  if (selected.length !== 1 || await setupStatus() !== "ok" || selection !== selectionEpoch) return {status: "window_snapshot_unavailable"};
  if (expired(expires)) return {status: "timeout"};
  const activation = lastActivation;
  try {
    const tab = await browser.tabs.create({windowId: window.id, active: false, url: target.href});
    if (expired(expires) || !regular(tab) || tab.windowId !== window.id || tab.active !== false ||
        tab.id === selected[0].id || window.tabs.some(existing => existing.id === tab.id) || tabs.has(tab.id)) return {status: "open_unconfirmed"};
    const snapshot = await browser.windows.get(window.id, {populate: true});
    if (!normal(snapshot) || snapshot.id !== window.id || !Array.isArray(snapshot.tabs) ||
        snapshot.focused !== window.focused) return {status: "open_unconfirmed"};
    const current = snapshot.tabs.find(item => item.id === tab.id);
    const active = snapshot.tabs.filter(item => item.active === true);
    if (lastActivation !== activation || selection !== selectionEpoch || expired(expires) || !regular(current) || current.windowId !== window.id || current.active !== false ||
        active.length !== 1 || active[0].id !== selected[0].id) return {status: "open_unconfirmed"};
    const description = describe(current, observed(current) ?? target);
    if (!description) return {status: "open_unconfirmed"};
    const ownership = crypto.randomUUID();
    refs.get(description.ref).ownership = {token: ownership, window: window.id};
    return {status: "ok", tab: description, created: true, ownership};
  } catch (_) { return {status: "open_unconfirmed"}; }
}

async function closeOwned({ref, ownership}, expires) {
  if (!ref.startsWith(instance + ".")) return {status: "cleanup_instance_changed"};
  const entry = refs.get(ref);
  if (!entry) return {status: "cleanup_tab_not_found"};
  if (entry.ownership?.token !== ownership) return {status: "cleanup_unconfirmed"};
  const original = entry.ownership;
  delete entry.ownership; // Consume before any async action; never replay an ambiguous removal.
  if (await setupStatus() !== "ok") return {status: "cleanup_unconfirmed"};
  const activation = lastActivation;
  const tab = await currentTab(entry);
  if (!tab || tab.windowId !== original.window || tab.active !== false ||
      refs.get(ref) !== entry || lastActivation !== activation || expired(expires)) return {status: "cleanup_unconfirmed"};
  try {
    await browser.tabs.remove(tab.id);
    forget(tab.id);
    return {status: lastActivation === activation ? "closed" : "cleanup_unconfirmed"};
  } catch (_) { return {status: "cleanup_unconfirmed"}; }
}

async function execute(request) {
  const {operation, payload, expires} = request;
  if (!validPayload(operation, payload) || !Number.isSafeInteger(expires)) return {status: "invalid_request"};
  if (expired(expires)) return {status: "timeout"};
  const selection = selectionEpoch;
  const status = await setupStatus();
  if (selection !== selectionEpoch) return {status: "disconnected"};
  if (status !== "ok") return {status};
  if (expired(expires)) return {status: "timeout"};
  if (operation === "status") return {status: "ok"};
  if (operation === "read") return read(payload, expires);
  if (operation === "close") return closeOwned(payload, expires);
  return findOpen(operation, payload, expires);
}

function bound(answer, operation) {
  const size = () => JSON.stringify(answer).replace(/[\/<>&\x7f-\uffff]/g, "\\u0000").length;
  if (size() <= limit) return answer;
  if (typeof answer.text === "string") {
    answer.truncated = true;
    while (answer.links?.length && size() > limit) answer.links.pop();
    while (answer.text.length && size() > limit) answer.text = answer.text.slice(0, Math.floor(answer.text.length * 0.75));
    if (answer.text && size() <= limit) return answer;
  }
  return {v: 1, id: answer.id, status: operation === "open" && answer.created ? "open_unconfirmed" : "response_too_large"};
}

async function poll(profile) {
  const selection = selectionEpoch;
  if (browser.extension?.inIncognitoContext !== false || await browser.extension?.isAllowedIncognitoAccess?.() !== false) return;
  if (profile !== enabledProfile) return;
  const grant = await native("pull", {selectedProfile: profile});
  const request = grant?.request;
  if (grant?.status !== "claimed" || grant.profile?.toLowerCase() !== profile || grant.instance?.toLowerCase() !== instance ||
      request?.v !== 1 || typeof request.id !== "string" || !requestID.test(request.id)) return;
  if (selection !== selectionEpoch) return;
  let result;
  try { result = await execute(request); }
  catch (_) { result = {status: request.operation === "close" ? "cleanup_unconfirmed" : "browser_error"}; }
  if (selection !== selectionEpoch) result = {status: request.operation === "open" ? "open_unconfirmed" :
    request.operation === "close" ? "cleanup_unconfirmed" : "disconnected"};
  await native("result", {selectedProfile: profile, id: request.id,
    answer: bound({v: 1, id: request.id, ...result}, request.operation)});
}

function schedule(delay = interval) {
  if (!enabledProfile || polling || timer !== undefined) return;
  timer = setTimeout(async () => {
    timer = undefined;
    if (!enabledProfile) return;
    polling = true;
    try { await poll(enabledProfile); } catch (_) { /* A later tick never replays a claim. */ }
    finally { polling = false; schedule(); }
  }, delay);
}

function configure(profile) {
  if (enabledProfile !== profile) { selectionEpoch++; readPinQualified = false; refs.clear(); tabs.clear(); }
  enabledProfile = typeof profile === "string" && uuid.test(profile) ? profile.toLowerCase() : undefined;
  if (!enabledProfile && timer !== undefined) { clearTimeout(timer); timer = undefined; }
  schedule(0);
}

const ready = browser.storage.local.get("enabledProfile").then(value => configure(value.enabledProfile));
ready.catch(() => {});
async function setup(command) {
  await ready;
  if (command === "disable") {
    configure(undefined);
    await browser.storage.local.remove("enabledProfile");
    return {status: "disabled"};
  }
  const identity = await native("identify");
  if (identity?.status !== "identified" || !uuid.test(identity.profile)) return {status: identity?.status ?? "identity_unavailable"};
  const profile = identity.profile.toLowerCase();
  if (command === "enable") {
    const status = await permissionsStatus();
    if (status !== "ok") return {status, profile};
    await browser.storage.local.set({enabledProfile: profile});
    configure(profile);
  }
  return {status: enabledProfile === profile ? "enabled" : "identified", profile};
}

// No external/content-page message handler or command entry. Only the exact bundled popup can opt in/out.
let setupQueue = Promise.resolve();
browser.runtime.onMessage.addListener((request, sender, respond) => {
  if (sender.id !== browser.runtime.id || sender.url !== browser.runtime.getURL("setup.html") ||
      !["identify", "enable", "disable"].includes(request)) return false;
  setupQueue = setupQueue.then(() => setup(request)).then(respond, () => respond({status: "setup_unavailable"}));
  return true;
});
