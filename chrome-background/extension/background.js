importScripts("read-page.js");

const host = "com.agent_browser.background";
const reconnect = "reconnect-native-host";
const version = 1;
const frameLimit = 65536;
let port;

async function openBackground({ url, expires }) {
  if (typeof url !== "string" || !/^https?:\/\//i.test(url)) {
    throw new Error("Chrome background open requires an HTTP(S) URL");
  }
  const target = new URL(url);
  if (target.username || target.password) {
    throw new Error("URLs with embedded credentials are not supported");
  }

  const tabs = await chrome.tabs.query({ windowType: "normal" });
  const match = tabs.find(tab => !tab.incognito &&
    (tab.pendingUrl ?? tab.url) === target.href);
  if (match) return { id: match.id, action: "reused" };

  const normal = { windowTypes: ["normal"] };
  const windows = (await chrome.windows.getAll(normal)).filter(window => !window.incognito);
  if (!windows.length) {
    if (!Number.isFinite(expires) || Date.now() >= expires) {
      throw new Error("Chrome background request expired before opening a tab");
    }
    const created = await chrome.windows.create({ url: target.href, type: "normal", incognito: false, focused: false });
    const id = created?.tabs?.[0]?.id;
    if (!Array.isArray(created?.tabs) || created.tabs.length !== 1 || !Number.isSafeInteger(id) || id < 0) {
      throw new Error("Chrome did not return a single created tab with a valid integer ID; inspect Chrome before retrying");
    }
    return { id, action: "created" };
  }
  const lastFocused = await chrome.windows.getLastFocused(normal).catch(() => null);
  const window = windows.find(window => window.id === lastFocused?.id) ?? windows[0];
  if (!Number.isFinite(expires) || Date.now() >= expires) {
    throw new Error("Chrome background request expired before opening a tab");
  }
  const tab = await chrome.tabs.create({ windowId: window.id, url: target.href, active: false });
  return { id: tab.id, action: "created" };
}

function httpURL(value) {
  if (typeof value !== "string" || !/^https?:\/\//i.test(value) || /[\\\s\x00-\x1f\x7f]/u.test(value)) return null;
  try {
    const url = new URL(value);
    return url.username || url.password ? null : url;
  } catch (_) {
    return null;
  }
}

function regular(tab) {
  return tab && tab.incognito === false && Number.isSafeInteger(tab.id) && tab.id >= 0;
}

function observedURL(tab) {
  return httpURL(tab.pendingUrl ?? tab.url);
}

function stateFor(connection) {
  const generation = crypto.randomUUID();
  const refs = new Map();
  const tabs = new Map();
  return {
    active: () => port === connection,
    inGeneration: ref => ref.startsWith(generation + "."),
    tab: ref => tabs.get(ref),
    describe(tab, url) {
      let entry = refs.get(tab.id);
      if (!entry) {
        entry = { id: tab.id, ref: `${generation}.${crypto.randomUUID()}`, navigation: 0 };
        refs.set(tab.id, entry);
        tabs.set(entry.ref, entry);
      }
      return { ref: entry.ref, url: url.href, title: (tab.title ?? "").slice(0, 500) };
    },
    forget(id) {
      const entry = refs.get(id);
      if (entry) tabs.delete(entry.ref);
      refs.delete(id);
    },
    navigated(id) {
      const entry = refs.get(id);
      if (entry) entry.navigation++;
    },
  };
}

async function currentTab(state, entry) {
  if (!entry || !state.active() || state.tab(entry.ref) !== entry) return null;
  const tab = await chrome.tabs.get(entry.id).catch(() => null);
  if (!regular(tab)) return null;
  const window = await chrome.windows.get(tab.windowId).catch(() => null);
  return state.active() && state.tab(entry.ref) === entry && window?.type === "normal" &&
    window.incognito === false && tab.id === entry.id ? tab : null;
}

function changed(url) {
  return { status: "target_changed", ...(url ? { final_url: url.href } : {}) };
}

function boundary(tab, expected) {
  if (!tab) return changed();
  const observed = observedURL(tab);
  if (observed?.href !== expected) return changed(observed);
  if (tab.pendingUrl !== undefined || httpURL(tab.url)?.href !== expected) return { status: "page_loading", final_url: expected };
  if (tab.discarded) return { status: "page_loading", final_url: expected };
  return null;
}

async function topDocument(tabId) {
  return chrome.webNavigation.getFrame({ tabId, frameId: 0 }).catch(() => null);
}

function activeDocument(frame, expected) {
  return frame?.documentLifecycle === "active" && typeof frame.documentId === "string" &&
    frame.documentId.length > 0 && frame.errorOccurred === false && frame.url === expected;
}

const readStatuses = new Set([
  "ok", "verification_required", "passive_challenge", "login_required", "page_loading",
  "content_not_ready", "invalid_wait_selector", "invalid_selector", "no_match", "no_content",
]);

async function guardedRead(state, data, expires) {
  const expected = httpURL(data.expected_url).href;
  const entry = state.tab(data.ref);
  const navigation = entry?.navigation;
  const before = await currentTab(state, entry);
  const mismatch = boundary(before, expected);
  if (mismatch) return mismatch;
  const frame = await topDocument(entry.id);
  if (frame && frame.url !== expected) return changed(httpURL(frame.url));
  // Chrome 120+ supports both documentIds injection and webNavigation document IDs.
  // If a document ID is unavailable, never fall back to frame-only injection.
  if (!activeDocument(frame, expected)) return { status: "read_unavailable", final_url: expected };
  if (!state.active() || state.tab(data.ref) !== entry || entry.navigation !== navigation) return changed();
  if (Date.now() >= expires) return { status: "timeout" };

  let results;
  try {
    results = await chrome.scripting.executeScript({
      target: { tabId: entry.id, documentIds: [frame.documentId] },
      world: "ISOLATED",
      func: readPage,
      args: [{ expectedURL: expected, ...(data.selector !== undefined ? { selector: data.selector } : {}),
        ...(data.wait_for_selector !== undefined ? { waitForSelector: data.wait_for_selector } : {}),
        includeLinks: data.include_links === true }],
    });
  } catch (_) {
    // A disappearing document and an injection permission failure both return no content.
  }

  const after = await currentTab(state, entry);
  const postMismatch = boundary(after, expected);
  if (postMismatch) return postMismatch;
  const postFrame = await topDocument(entry.id);
  if (!state.active() || state.tab(data.ref) !== entry || entry.navigation !== navigation || !activeDocument(postFrame, expected) ||
      postFrame.documentId !== frame.documentId) return changed(httpURL(postFrame?.url));
  if (Date.now() >= expires) return { status: "timeout" };
  if (!results) return { status: "read_unavailable", final_url: expected };
  const probe = results[0]?.result;
  if (results.length !== 1 || results[0].documentId !== frame.documentId || results[0].frameId !== 0) return changed();
  if (probe?.status === "target_changed") return changed(httpURL(probe.document_url));
  if (!readStatuses.has(probe?.status) || (probe.status === "ok" && probe.final_url !== expected)) return changed();
  return { ...probe, final_url: expected };
}

async function findOrOpen(state, operation, data, expires) {
  const target = httpURL(data.url);
  const tabs = await chrome.tabs.query({ windowType: "normal" });
  if (!state.active()) return { status: "disconnected" };
  if (Date.now() >= expires) return { status: "timeout" };
  const candidates = [];
  for (const tab of tabs) {
    if (!regular(tab)) continue;
    const url = observedURL(tab);
    if (!url || (operation === "find" ? url.origin !== target.origin || url.pathname !== target.pathname : url.href !== target.href)) continue;
    candidates.push(state.describe(tab, url));
  }
  if (operation === "find") return { status: "ok", tabs: candidates };
  if (candidates.length > 1) return { status: "tab_ambiguous", tabs: candidates };
  if (candidates.length === 1) return { status: "ok", tab: candidates[0], created: false };

  const normal = { windowTypes: ["normal"] };
  const windows = (await chrome.windows.getAll(normal)).filter(window => window.incognito === false);
  const lastFocused = windows.length ? await chrome.windows.getLastFocused(normal).catch(() => null) : null;
  const window = windows.find(window => window.id === lastFocused?.id) ?? windows[0];
  if (!state.active()) return { status: "disconnected" };
  if (Date.now() >= expires) return { status: "timeout" };
  try {
    let tab;
    if (window) {
      tab = await chrome.tabs.create({ windowId: window.id, url: target.href, active: false });
    } else {
      const created = await chrome.windows.create({ url: target.href, type: "normal", incognito: false, focused: false });
      if (created?.incognito === false && created.tabs?.length === 1) tab = created.tabs[0];
    }
    if (!regular(tab) || !state.active()) return { status: "open_unconfirmed" };
    const description = state.describe(tab, observedURL(tab) ?? target);
    const ownership = crypto.randomUUID();
    state.tab(description.ref).ownership = ownership;
    return { status: "ok", tab: description, created: true, ownership };
  } catch (_) {
    return { status: "open_unconfirmed" };
  }
}

async function closeOwned(state, { ref, ownership }, expires) {
  if (!state.inGeneration(ref)) return { status: "cleanup_instance_changed" };
  const entry = state.tab(ref);
  if (!entry) return { status: "cleanup_tab_not_found" };
  if (entry.ownership !== ownership) return { status: "cleanup_unconfirmed" };
  delete entry.ownership; // There is no replay, even if the first attempt is uncertain.
  const tab = await currentTab(state, entry);
  if (!state.active() || Date.now() >= expires) return { status: "cleanup_unconfirmed" };
  if (state.tab(ref) !== entry) return { status: "cleanup_tab_not_found" };
  if (!tab) return { status: "cleanup_unconfirmed" };
  try {
    await chrome.tabs.remove(tab.id);
    state.forget(tab.id);
    return { status: "closed" };
  } catch (_) {
    return { status: "cleanup_unconfirmed" };
  }
}

const token = /^[\da-f-]{36}(?:\.[\da-f-]{36})?$/i;
const selector = value => typeof value === "string" && value.length > 0 && value.length <= 2000;

function validPayload(operation, data) {
  if (!data || typeof data !== "object" || Array.isArray(data)) return false;
  const only = (...keys) => Object.keys(data).every(key => keys.includes(key));
  const validToken = value => typeof value === "string" && token.test(value);
  if (operation === "status") return only();
  if (operation === "find" || operation === "open") return only("url") && !!httpURL(data.url);
  if (operation === "close") return only("ref", "ownership") && validToken(data.ref) && validToken(data.ownership);
  return operation === "read" && only("ref", "expected_url", "selector", "wait_for_selector", "include_links") &&
    validToken(data.ref) && !!httpURL(data.expected_url) &&
    (!("selector" in data) || selector(data.selector)) &&
    (!("wait_for_selector" in data) || selector(data.wait_for_selector)) &&
    (!("include_links" in data) || typeof data.include_links === "boolean");
}

async function operation(state, { operation, payload, expires }) {
  if (!validPayload(operation, payload) || !Number.isSafeInteger(expires)) return { status: "invalid_request" };
  if (Date.now() >= expires) return { status: operation === "close" ? "cleanup_unconfirmed" : "timeout" };
  if (operation !== "close" && !await chrome.permissions.contains({ origins: ["http://*/*", "https://*/*"] })) {
    return { status: "permission_required" };
  }
  if (operation === "status") return { status: "ok" };
  if (operation === "read") return guardedRead(state, payload, expires);
  if (operation === "close") return closeOwned(state, payload, expires);
  return findOrOpen(state, operation, payload, expires);
}

function withinBudget(response) {
  // The host re-encodes as ASCII JSON. Budget six bytes per non-ASCII UTF-16
  // unit (including either half of an emoji), and include HTML-sensitive escapes
  // that the browser's native serializer may add. JSON.stringify already escapes controls.
  const size = () => JSON.stringify(response).replace(/[<>&\x7f-\uffff]/g, "\\u0000").length;
  if (size() <= frameLimit) return response;
  if (response.status === "ok" && typeof response.text === "string") {
    response.truncated = true;
    while (response.links?.length && size() > frameLimit) response.links.pop();
    while (response.text.length && size() > frameLimit) response.text = response.text.slice(0, Math.floor(response.text.length * 0.75));
    if (response.text && size() <= frameLimit) return response;
  }
  const { v, id } = response;
  return { v, id, status: response.created ? "open_unconfirmed" : "response_too_large" };
}

let forgetTab = () => {};
let navigateTab = () => {};
chrome.tabs.onRemoved?.addListener(tabId => forgetTab(tabId));
chrome.tabs.onReplaced?.addListener((_added, removed) => forgetTab(removed));
for (const name of ["onBeforeNavigate", "onCommitted", "onHistoryStateUpdated", "onReferenceFragmentUpdated"]) {
  chrome.webNavigation?.[name]?.addListener(({tabId, frameId}) => { if (frameId === 0) navigateTab(tabId); });
}

function connect() {
  if (port || chrome.extension?.inIncognitoContext) return;
  const connection = chrome.runtime.connectNative(host);
  port = connection;
  const state = stateFor(connection);
  forgetTab = state.forget;
  navigateTab = state.navigated;
  connection.onMessage.addListener(async request => {
    let response;
    if (request?.v === undefined) {
      // Compatibility for the old background-open CLI only.
      try {
        response = await openBackground(request);
      } catch (error) {
        response = { error: error.message };
      }
    } else {
      const id = typeof request.id === "string" && request.id.length <= 36 ? request.id : "";
      let result;
      try {
        result = request.v === version ? await operation(state, request) : { status: "protocol_mismatch" };
      } catch (_) {
        result = { status: request.operation === "close" ? "cleanup_unconfirmed" : "browser_error" };
      }
      response = withinBudget({ v: version, id, ...result });
    }
    if (port === connection) connection.postMessage(response);
  });
  connection.onDisconnect.addListener(() => {
    // Reading lastError suppresses the warning when the host isn't installed yet.
    void chrome.runtime.lastError;
    if (port !== connection) return;
    port = undefined;
    forgetTab = () => {};
    navigateTab = () => {};
    chrome.alarms.create(reconnect, { delayInMinutes: 0.5 });
  });
}

chrome.alarms.onAlarm.addListener(alarm => {
  if (alarm.name === reconnect) connect();
});
chrome.runtime.onStartup.addListener(connect);
chrome.runtime.onInstalled.addListener(connect);
connect();
