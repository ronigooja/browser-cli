importScripts("read-page.js");

const host = "com.agent_browser.background";
const reconnect = "reconnect-native-host";
const version = 4;
const frameLimit = 65536;
const captureGrant = { origins: ["<all_urls>"] };
const captureChunkLimit = 48000;
const captureImageLimit = 12 * 1024 * 1024;
const captureImageTTL = 30000;
const pngPrefix = "data:image/png;base64,";
const pageKinds = new Set(["info", "snapshot", "html", "extract", "wait", "click", "scroll", "fill", "select", "press", "submit"]);
const pageWrites = new Set(["click", "scroll", "fill", "select", "press", "submit"]);
let port;
let uninstallDispatched = false;
let captureInFlight = false;

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
  const observers = new Set();
  const captureObservers = new Set();
  let transfer;
  function clearImage() {
    if (transfer) clearTimeout(transfer.timer);
    transfer = undefined;
  }
  return {
    active: () => port === connection && !uninstallDispatched,
    inGeneration: ref => ref.startsWith(generation + "."),
    tab: ref => tabs.get(ref),
    knows: id => refs.has(id),
    observe(fn) {
      observers.add(fn);
      return () => observers.delete(fn);
    },
    observeCapture(fn) {
      captureObservers.add(fn);
      return () => captureObservers.delete(fn);
    },
    notify(kind, windowId, tabId) {
      for (const observe of captureObservers) observe(kind, windowId, tabId);
    },
    beginCapture() {
      if (captureInFlight || transfer) return false;
      captureInFlight = true;
      return true;
    },
    finishCapture() { captureInFlight = false; },
    storeImage(image) {
      const image_ref = crypto.randomUUID();
      transfer = { image, image_ref, offset: 0, expires: Date.now() + captureImageTTL,
        timer: setTimeout(clearImage, captureImageTTL) };
      return { image_ref, image_length: image.length };
    },
    chunk({ image_ref, offset }) {
      if (transfer && Date.now() >= transfer.expires) clearImage();
      if (!transfer || transfer.image_ref !== image_ref || transfer.offset !== offset) return { status: "capture_transfer_unavailable" };
      const next_offset = Math.min(offset + captureChunkLimit, transfer.image.length);
      const data = transfer.image.slice(offset, next_offset);
      if (next_offset === transfer.image.length) clearImage();
      else transfer.offset = next_offset;
      return { status: "ok", data, next_offset };
    },
    clearImage,
    changed(kind, windowId, tabId) {
      for (const observe of observers) observe(kind, windowId, tabId);
      for (const observe of captureObservers) observe(kind, windowId, tabId);
      const entry = refs.get(tabId);
      if (entry) {
        delete entry.ownership;
        entry.adoption++;
      }
    },
    describe(tab, url) {
      let entry = refs.get(tab.id);
      if (!entry) {
        entry = { id: tab.id, ref: `${generation}.${crypto.randomUUID()}`, navigation: 0, adoption: 0 };
        refs.set(tab.id, entry);
        tabs.set(entry.ref, entry);
      }
      return { ref: entry.ref, url: url.href, title: (tab.title ?? "").slice(0, 500) };
    },
    forget(id, replaced = false) {
      for (const observe of observers) observe("identity", undefined, id);
      for (const observe of captureObservers) observe("identity", undefined, id);
      const entry = refs.get(id);
      if (entry) {
        if (replaced) entry.adoption++;
        tabs.delete(entry.ref);
      }
      refs.delete(id);
    },
    navigated(id) {
      const entry = refs.get(id);
      if (entry) entry.navigation++;
    },
  };
}

function normalWindow(window) {
  return window?.type === "normal" && window.incognito === false && Number.isSafeInteger(window.id) && window.id >= 0;
}

async function currentTab(state, entry) {
  if (!entry || !state.active() || state.tab(entry.ref) !== entry) return null;
  const tab = await chrome.tabs.get(entry.id).catch(() => null);
  if (!regular(tab)) return null;
  const window = await chrome.windows.get(tab.windowId).catch(() => null);
  return state.active() && state.tab(entry.ref) === entry && normalWindow(window) &&
    window.id === tab.windowId && tab.id === entry.id ? tab : null;
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

async function listTabs(state, expires) {
  const tabs = await chrome.tabs.query({ windowType: "normal" });
  const result = [];
  let truncated = false;
  for (const tab of tabs) {
    const url = httpURL(tab.url);
    if (!regular(tab) || !url || tab.pendingUrl !== undefined || typeof tab.title !== "string" ||
        typeof tab.active !== "boolean" || !Number.isSafeInteger(tab.windowId) || tab.windowId < 0) continue;
    const window = await chrome.windows.get(tab.windowId).catch(() => null);
    if (!normalWindow(window) || window.id !== tab.windowId) continue;
    if (result.length === 32 || tab.title.length > 500 || url.href.length > 4096) { truncated = true; continue; }
    const item = { ...state.describe(tab, url), active: tab.active, window_id: tab.windowId };
    if (JSON.stringify([...result, item]).replace(/[<>&\x7f-\uffff]/g, "\\u0000").length > 60000) { truncated = true; continue; }
    result.push(item);
  }
  if (!state.active()) return { status: "disconnected" };
  if (Date.now() >= expires) return { status: "timeout" };
  return { status: "ok", tabs: result, ...(truncated ? { truncated: true } : {}) };
}

async function guardedPage(state, data, expires) {
  const uncertain = { status: pageWrites.has(data.kind) ? "action_unconfirmed" : "read_unavailable" };
  const expected = httpURL(data.expected_url).href;
  const entry = state.tab(data.ref);
  const navigation = entry?.navigation;
  const mismatch = boundary(await currentTab(state, entry), expected);
  if (mismatch) return mismatch;
  const frame = await topDocument(entry.id);
  if (!activeDocument(frame, expected)) return { status: "read_unavailable", final_url: expected };
  if (!state.active() || state.tab(data.ref) !== entry || entry.navigation !== navigation) return changed();
  if (Date.now() >= expires) return { status: "timeout" };
  if (typeof agentBrowserPageOperation !== "function") return { status: "read_unavailable", final_url: expected };
  const { ref, expected_url, ...options } = data;
  let results;
  try {
    results = await chrome.scripting.executeScript({ target: { tabId: entry.id, documentIds: [frame.documentId] },
      world: "ISOLATED", func: agentBrowserPageOperation, args: [{ ...options, expectedURL: expected }] });
  } catch (_) { return uncertain; }
  const postMismatch = boundary(await currentTab(state, entry), expected);
  const postFrame = await topDocument(entry.id);
  if (postMismatch || !state.active() || state.tab(ref) !== entry || entry.navigation !== navigation ||
      !activeDocument(postFrame, expected) || postFrame.documentId !== frame.documentId || Date.now() >= expires ||
      results?.length !== 1 || results[0].frameId !== 0 || results[0].documentId !== frame.documentId) {
    return uncertain;
  }
  const result = results[0].result;
  if (!result || typeof result !== "object" || Array.isArray(result) || typeof result.status !== "string" ||
      (result.final_url !== undefined && result.final_url !== expected)) return uncertain;
  return { ...result, final_url: expected };
}

function capturePermissionRequired() {
  return { status: "capture_permission_required", message: "In the intended Chrome profile, open the Extensions toolbar menu, click Agent Browser (or its pinned toolbar button), and approve the one-time permission. No per-site grant is needed." };
}

function captureCrop(crop) {
  const keys = ["x", "y", "width", "height", "viewport_width", "viewport_height"];
  if (!crop || !keys.every(key => Number.isFinite(crop[key]))) return null;
  const { x, y, width, height, viewport_width, viewport_height } = crop;
  if (x < 0 || y < 0 || width <= 0 || height <= 0 || x + width > viewport_width + 0.000001 ||
      y + height > viewport_height + 0.000001 || viewport_width <= 0 || viewport_height <= 0) return null;
  return { x, y, width, height, viewport_width, viewport_height };
}

async function guardedCapture(state, data, expires) {
  const expected = httpURL(data.expected_url).href;
  const entry = state.tab(data.ref);
  if (!entry) return changed();
  const navigation = entry.navigation;
  const unavailable = { status: "capture_unavailable", final_url: expected };
  if (!state.beginCapture()) return unavailable;
  let invalidated = false;
  let windowId;
  // A switch away and back invalidates the capture even if both snapshots match.
  const stop = state.observeCapture((kind, id, tabId) => {
    if (kind === "focus" || kind === "permission" || (kind === "window_removed" && id === windowId) ||
        (kind === "activation" && (windowId === undefined || id === windowId)) || tabId === entry.id) invalidated = true;
  });
  const stale = () => invalidated || !state.active() || state.tab(data.ref) !== entry || entry.navigation !== navigation;
  const expired = () => Date.now() >= expires;
  let frame;
  async function pinned() {
    if (stale()) return changed();
    if (expired()) return { status: "timeout" };
    const tab = await chrome.tabs.get(entry.id).catch(() => null);
    if (!regular(tab) || tab.id !== entry.id || (windowId !== undefined && tab.windowId !== windowId)) return changed();
    const mismatch = boundary(tab, expected);
    if (mismatch) return mismatch;
    if (!Number.isSafeInteger(tab.windowId) || tab.windowId < 0) return unavailable;
    windowId = tab.windowId;
    const currentFrame = await topDocument(tab.id);
    if (currentFrame && currentFrame.url !== expected) return changed(httpURL(currentFrame.url));
    if (!activeDocument(currentFrame, expected)) return unavailable;
    if (frame && currentFrame.documentId !== frame.documentId) return changed();
    frame = currentFrame;
    const window = await chrome.windows.get(windowId, { populate: true }).catch(() => null);
    if (stale()) return changed();
    if (expired()) return { status: "timeout" };
    if (tab.active !== true || !normalWindow(window) || window.id !== windowId || window.focused !== true ||
        !Array.isArray(window.tabs)) return unavailable;
    const selected = window.tabs.filter(item => item.active === true);
    if (selected.length !== 1 || !regular(selected[0]) || selected[0].id !== tab.id || selected[0].windowId !== windowId) return changed();
    return boundary(selected[0], expected);
  }
  async function geometry() {
    let results;
    try {
      results = await chrome.scripting.executeScript({ target: { tabId: entry.id, documentIds: [frame.documentId] },
        world: "ISOLATED", func: agentBrowserPageOperation,
        args: [{ kind: "capture_geometry", expectedURL: expected, ...(data.selector === undefined ? {} : { selector: data.selector }) }] });
    } catch (_) { return unavailable; }
    if (stale()) return changed();
    if (expired()) return { status: "timeout" };
    if (results?.length !== 1 || results[0].frameId !== 0 || results[0].documentId !== frame.documentId) return changed();
    const probe = results[0].result;
    if (probe?.status === "target_changed") return changed(httpURL(probe.final_url));
    if (probe?.final_url !== expected || typeof probe.status !== "string" || !/^[a-z_]{2,40}$/.test(probe.status)) return unavailable;
    if (probe.status !== "ok") return { status: probe.status, final_url: expected };
    const crop = captureCrop(probe.crop);
    if (!crop) return unavailable;
    return { status: "ok", crop };
  }
  try {
    if (!await chrome.permissions.contains({ origins: ["http://*/*", "https://*/*"] })) return { status: "permission_required" };
    if (!await chrome.permissions.contains(captureGrant)) return capturePermissionRequired();
    const preflight = await pinned();
    if (preflight) return preflight;
    const before = await geometry();
    if (before.status !== "ok") return before;
    const preCapture = await pinned();
    if (preCapture) return preCapture;
    // captureVisibleTab targets a window, not a document. Never release its
    // pixels until the document, active tab and focus are rechecked.
    let image;
    try { image = await chrome.tabs.captureVisibleTab(windowId, { format: "png" }); }
    catch (_) { return unavailable; }
    const postCapture = await pinned();
    if (postCapture) return postCapture;
    const after = await geometry();
    if (after.status !== "ok") return after;
    const postflight = await pinned();
    if (postflight) return postflight;
    if (JSON.stringify(before.crop) !== JSON.stringify(after.crop)) return unavailable;
    if (typeof image !== "string" || image.length > captureImageLimit || !image.startsWith(pngPrefix) ||
        (image.length - pngPrefix.length) % 4 || !/^[A-Za-z0-9+/]+={0,2}$/.test(image.slice(pngPrefix.length))) return unavailable;
    const result = { status: "ok", final_url: expected, ...(data.selector !== undefined ? { crop: before.crop } : {}) };
    // Leave room for transport fields and worst-case ASCII JSON URL expansion.
    if (image.length <= captureChunkLimit && expected.length <= 2048) return { ...result, image };
    return { ...result, ...state.storeImage(image) };
  } finally {
    stop();
    state.finishCapture();
  }
}

async function tabOperation(state, data, expires) {
  const expected = httpURL(data.expected_url).href;
  const entry = state.tab(data.ref);
  const navigation = entry?.navigation;
  const beforeFrame = entry && await topDocument(entry.id);
  const guard = await guardedPage(state, { ref: data.ref, expected_url: expected, kind: "info" }, expires);
  if (guard.status !== "ok") return guard;
  const tab = await currentTab(state, entry);
  const mismatch = boundary(tab, expected);
  if (mismatch) return mismatch;
  const frame = await topDocument(entry.id);
  if (!activeDocument(beforeFrame, expected) || !activeDocument(frame, expected) ||
      frame.documentId !== beforeFrame.documentId || !state.active() || state.tab(data.ref) !== entry ||
      entry.navigation !== navigation || Date.now() >= expires) return changed();
  try {
    delete entry.ownership;
    if (data.kind === "navigate") {
      await chrome.tabs.update(entry.id, { url: httpURL(data.url).href });
      const after = await currentTab(state, entry);
      const url = observedURL(after);
      if (!after || Date.now() >= expires) return { status: "action_unconfirmed" };
      if (url?.href !== httpURL(data.url).href) return { status: "ok" };
      return { status: "ok", tab: state.describe(after, url) };
    }
    if (data.kind === "activate") {
      await chrome.tabs.update(entry.id, { active: true });
      await chrome.windows.update(tab.windowId, { focused: true });
      const after = await currentTab(state, entry);
      const postFrame = await topDocument(entry.id);
      const window = await chrome.windows.get(tab.windowId).catch(() => null);
      if (boundary(after, expected) || !after.active || !window?.focused || !activeDocument(postFrame, expected) ||
          postFrame.documentId !== frame.documentId || entry.navigation !== navigation || Date.now() >= expires) return { status: "action_unconfirmed" };
      return { status: "ok", tab: state.describe(after, httpURL(after.url)) };
    }
    await chrome.tabs.remove(entry.id);
    if (!state.active() || Date.now() >= expires) return { status: "action_unconfirmed" };
    state.forget(entry.id);
    return { status: "ok" };
  } catch (_) { return { status: "action_unconfirmed" }; }
}

// Explicit foreground action only: never called as a background-open fallback.
async function frontOpen(state, data, expires) {
  const target = httpURL(data.url);
  const candidates = [];
  for (const tab of await chrome.tabs.query({ windowType: "normal" })) {
    if (!regular(tab) || observedURL(tab)?.href !== target.href) continue;
    const window = await chrome.windows.get(tab.windowId).catch(() => null);
    if (normalWindow(window) && window.id === tab.windowId) candidates.push(state.describe(tab, target));
  }
  if (!state.active()) return { status: "disconnected" };
  if (Date.now() >= expires) return { status: "timeout" };
  if (candidates.length > 1) return { status: "tab_ambiguous", tabs: candidates };
  if (candidates.length === 1) {
    // Opening a URL is not consent to activate somebody's existing tab.
    return { status: "ok", tab: candidates[0], created: false };
  }
  const windows = await chrome.windows.getAll({ populate: true });
  const eligible = windows.filter(normalWindow);
  const focused = focusedWindow(windows);
  const last = eligible.length > 1 ? await chrome.windows.getLastFocused({ windowTypes: ["normal"] }).catch(() => null) : null;
  const window = eligible.find(item => item.id === focused) ?? eligible.find(item => item.id === last?.id) ??
    (eligible.length === 1 ? eligible[0] : null);
  if (!window || !windowSelection(window) || focused === undefined) return { status: "window_snapshot_unavailable" };
  if (!state.active()) return { status: "disconnected" };
  if (Date.now() >= expires) return { status: "timeout" };
  let dispatched = false;
  const events = [];
  const stop = state.observe((...event) => { if (events.length < 32) events.push(event); });
  try {
    dispatched = true;
    const tab = await chrome.tabs.create({ windowId: window.id, url: target.href, active: true });
    if (!regular(tab) || tab.windowId !== window.id || tab.active !== true || state.knows(tab.id) ||
        window.tabs.some(item => item.id === tab.id) || observedURL(tab)?.href !== target.href) return { status: "open_unconfirmed" };
    await chrome.windows.update(window.id, { focused: true });
    const current = await chrome.tabs.get(tab.id).catch(() => null);
    const actualWindow = await chrome.windows.get(window.id).catch(() => null);
    if (!state.active() || Date.now() >= expires || !regular(current) || current.id !== tab.id ||
        current.windowId !== window.id || current.active !== true || observedURL(current)?.href !== target.href ||
        !normalWindow(actualWindow) || actualWindow.id !== window.id || actualWindow.focused !== true ||
        events.length === 32 || events.some(([kind, , id]) => kind === "identity" && id === tab.id)) return { status: "open_unconfirmed" };
    return { status: "ok", tab: state.describe(current, target), created: true };
  } catch (_) { return { status: dispatched ? "open_unconfirmed" : "window_snapshot_unavailable" }; }
  finally { stop(); }
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

  return openBackground(state, target, expires);
}

function focusedWindow(windows) {
  if (windows.some(window => typeof window.focused !== "boolean")) return undefined;
  const focused = windows.filter(window => window.focused);
  if (!focused.length) return null;
  if (focused.length === 1 && Number.isSafeInteger(focused[0].id) && focused[0].id >= 0) return focused[0].id;
}

function windowSelection(window) {
  if (!Array.isArray(window.tabs) || window.tabs.some(tab => !regular(tab) || tab.windowId !== window.id ||
      typeof tab.active !== "boolean" || typeof tab.highlighted !== "boolean")) return null;
  const active = window.tabs.filter(tab => tab.active);
  if (active.length !== 1) return null;
  return { active: active[0].id, highlighted: window.tabs.filter(tab => tab.highlighted).map(tab => tab.id) };
}

async function openBackground(state, target, expires) {
  const snapshot = () => chrome.windows.getAll({ populate: true });
  const uncertain = { status: "open_unconfirmed" };
  const unavailable = { status: "window_snapshot_unavailable" };
  const events = [];
  let dispatched = false;
  const stop = state.observe((...event) => { if (events.length < 32) events.push(event); });
  try {
    const before = await snapshot();
    const focused = focusedWindow(before);
    const eligible = before.filter(normalWindow);
    if (!eligible.length && before.length) return { status: "no_regular_window" };
    const lastFocused = eligible.length > 1 ? await chrome.windows.getLastFocused({ windowTypes: ["normal"] }).catch(() => null) : null;
    const window = eligible.find(item => item.id === focused) ?? eligible.find(item => item.id === lastFocused?.id) ??
      (eligible.length === 1 ? eligible[0] : null);
    const selected = window && windowSelection(window);
    if (!state.active()) return { status: "disconnected" };
    if (Date.now() >= expires) return { status: "timeout" };
    if (events.length || focused === undefined || (eligible.length && !selected)) return unavailable;

    dispatched = true;
    let tab;
    let windowId;
    if (window) {
      windowId = window.id;
      tab = await chrome.tabs.create({ windowId, url: target.href, active: false });
    } else {
      const created = await chrome.windows.create({ url: target.href, type: "normal", incognito: false, focused: false });
      if (!normalWindow(created) || created.focused !== false || created.tabs?.length !== 1) return uncertain;
      windowId = created.id;
      tab = created.tabs[0];
    }
    const expectedActive = !window;
    if (!regular(tab) || tab.windowId !== windowId || tab.active !== expectedActive ||
        (window && tab.highlighted !== false) || window?.tabs.some(item => item.id === tab.id) ||
        state.knows(tab.id)) return uncertain;

    const after = await snapshot();
    const destination = after.find(item => item.id === windowId);
    const actualSelection = normalWindow(destination) && windowSelection(destination);
    const current = actualSelection && destination.tabs.find(item => item.id === tab.id);
    // The first tab of a new window must be selected even when the window is unfocused.
    // Only that initial selection is expected; do not grant ownership of an active tab.
    const unexpectedEvent = events.length === 32 || events.some(([kind, id, tabId]) =>
      kind === "focus" || (kind === "selection" ? (window || id !== windowId || tabId !== tab.id) : tabId === tab.id));
    if (unexpectedEvent || !state.active() || Date.now() >= expires || focusedWindow(after) !== focused ||
        !regular(current) || current.windowId !== windowId || current.active !== expectedActive ||
        actualSelection.active !== (selected?.active ?? tab.id)) return uncertain;
    const highlighted = selected?.highlighted ?? [tab.id];
    if (actualSelection.highlighted.length !== highlighted.length ||
        actualSelection.highlighted.some(id => !highlighted.includes(id)) ||
        (!window && (after.length !== 1 || destination.tabs.length !== 1))) return uncertain;

    const description = state.describe(current, observedURL(current) ?? target);
    if (!window) return { status: "ok", tab: description, created: true };
    const ownership = crypto.randomUUID();
    state.tab(description.ref).ownership = { token: ownership, windowId };
    return { status: "ok", tab: description, created: true, ownership };
  } catch (_) {
    return dispatched ? uncertain : unavailable;
  } finally {
    stop();
  }
}

async function closeOwned(state, { ref, ownership }, expires) {
  if (!state.inGeneration(ref)) return { status: "cleanup_instance_changed" };
  const entry = state.tab(ref);
  if (!entry) return { status: "cleanup_tab_not_found" };
  const original = entry.ownership;
  if (original?.token !== ownership) return { status: "cleanup_unconfirmed" };
  delete entry.ownership; // There is no replay, even if the first attempt is uncertain.
  const adoption = entry.adoption;
  const window = await chrome.windows.get(original.windowId).catch(() => null);
  if (!normalWindow(window) || window.id !== original.windowId) return { status: "cleanup_unconfirmed" };
  const tab = await chrome.tabs.get(entry.id).catch(() => null);
  if (!state.active() || Date.now() >= expires) return { status: "cleanup_unconfirmed" };
  if (state.tab(ref) !== entry) return { status: "cleanup_tab_not_found" };
  if (entry.adoption !== adoption || !regular(tab) || tab.id !== entry.id || tab.windowId !== original.windowId ||
      tab.active !== false || tab.highlighted !== false) return { status: "cleanup_unconfirmed" };
  try {
    // Chrome has no atomic remove-if-inactive operation. Keep the last tab check
    // adjacent to dispatch, and report any observed adoption during it as uncertain.
    await chrome.tabs.remove(tab.id);
    state.forget(tab.id);
    return { status: entry.adoption === adoption && state.active() ? "closed" : "cleanup_unconfirmed" };
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
  if (operation === "list") return only();
  if (operation === "uninstall") return only();
  if (operation === "capture") return only("ref", "expected_url", "selector") && validToken(data.ref) &&
    httpURL(data.expected_url)?.href.length <= 4096 && (!("selector" in data) || selector(data.selector));
  if (operation === "capture_chunk") return only("image_ref", "offset") && typeof data.image_ref === "string" &&
    data.image_ref.length === 36 && validToken(data.image_ref) && Number.isSafeInteger(data.offset) &&
    data.offset >= 0 && data.offset < captureImageLimit;
  if (operation === "page") return only("ref", "expected_url", "kind", "selector", "elementRef", "text", "key", "value", "direction", "pixels", "condition", "offset", "raw") &&
    validToken(data.ref) && !!httpURL(data.expected_url) && pageKinds.has(data.kind) &&
    ["selector", "elementRef", "text", "key", "value", "direction", "condition"].every(key => !(key in data) || (typeof data[key] === "string" && data[key].length <= 10000)) &&
    ["pixels", "offset"].every(key => !(key in data) || Number.isSafeInteger(data[key])) &&
    (!("raw" in data) || typeof data.raw === "boolean");
  if (operation === "tab") return only("ref", "expected_url", "kind", "url") && validToken(data.ref) && !!httpURL(data.expected_url) &&
    ["navigate", "activate", "close"].includes(data.kind) && (data.kind === "navigate" ? !!httpURL(data.url) : !("url" in data));
  if (operation === "find" || operation === "open" || operation === "front_open") return only("url") && !!httpURL(data.url);
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
  if (operation === "uninstall") {
    if (uninstallDispatched) return { status: "uninstall_unverified" };
    if (!state.active() || typeof chrome.management?.uninstallSelf !== "function") return { status: "uninstall_unavailable" };
    uninstallDispatched = true;
    state.clearImage();
    return { status: "uninstall_dispatched" };
  }
  if (uninstallDispatched) return { status: "disconnected" };
  if (operation === "capture") return guardedCapture(state, payload, expires);
  if (operation === "capture_chunk") return state.active() ? state.chunk(payload) : { status: "capture_transfer_unavailable" };
  if (operation !== "close" && !await chrome.permissions.contains({ origins: ["http://*/*", "https://*/*"] })) {
    return { status: "permission_required" };
  }
  if (operation === "status") return { status: "ok" };
  if (operation === "list") return listTabs(state, expires);
  if (operation === "page") return guardedPage(state, payload, expires);
  if (operation === "tab") return tabOperation(state, payload, expires);
  if (operation === "front_open") return frontOpen(state, payload, expires);
  if (operation === "read") return guardedRead(state, payload, expires);
  if (operation === "close") return closeOwned(state, payload, expires);
  return findOrOpen(state, operation, payload, expires);
}

function withinBudget(response, operation, kind) {
  // The host re-encodes as ASCII JSON. Budget six bytes per non-ASCII UTF-16
  // unit (including either half of an emoji), and include HTML-sensitive escapes
  // that the browser's native serializer may add. JSON.stringify already escapes controls.
  const size = () => JSON.stringify(response).replace(/[<>&\x7f-\uffff]/g, "\\u0000").length;
  if (size() <= frameLimit) return response;
  if (operation === "read" && response.status === "ok" && typeof response.text === "string") {
    response.truncated = true;
    while (response.links?.length && size() > frameLimit) response.links.pop();
    while (response.text.length && size() > frameLimit) response.text = response.text.slice(0, Math.floor(response.text.length * 0.75));
    if (response.text && size() <= frameLimit) return response;
  }
  const { v, id } = response;
  return { v, id, status: response.created || operation === "front_open" ? "open_unconfirmed" :
    operation === "tab" || (operation === "page" && pageWrites.has(kind)) ? "action_unconfirmed" : "response_too_large" };
}

let forgetTab = () => {};
let navigateTab = () => {};
let changedTab = () => {};
let observeEvent = () => {};
let clearCaptureImage = () => {};
chrome.tabs.onRemoved?.addListener(tabId => forgetTab(tabId));
chrome.tabs.onReplaced?.addListener((_added, removed) => forgetTab(removed, true));
chrome.tabs.onActivated.addListener(({ tabId, windowId }) => {
  changedTab("selection", windowId, tabId);
  observeEvent("activation", windowId, tabId);
});
chrome.tabs.onHighlighted.addListener(({ tabIds, windowId }) => {
  if (!tabIds.length) changedTab("selection", windowId);
  for (const tabId of tabIds) changedTab("selection", windowId, tabId);
});
for (const name of ["onAttached", "onDetached", "onMoved"]) {
  chrome.tabs[name].addListener(tabId => changedTab("adoption", undefined, tabId));
}
chrome.tabs.onUpdated.addListener((tabId, change) => {
  if ("pinned" in change || "groupId" in change) changedTab("adoption", undefined, tabId);
  if ("url" in change || "pendingUrl" in change || change.status === "loading" || change.discarded === true) observeEvent("capture_identity", undefined, tabId);
});
chrome.windows.onFocusChanged.addListener(windowId => changedTab("focus", windowId));
chrome.windows.onRemoved?.addListener(windowId => observeEvent("window_removed", windowId));
for (const name of ["onBeforeNavigate", "onCommitted", "onHistoryStateUpdated", "onReferenceFragmentUpdated"]) {
  chrome.webNavigation?.[name]?.addListener(({tabId, frameId}) => { if (frameId === 0) navigateTab(tabId); });
}

function connect() {
  if (port || uninstallDispatched || chrome.extension?.inIncognitoContext) return;
  const connection = chrome.runtime.connectNative(host);
  port = connection;
  const state = stateFor(connection);
  forgetTab = state.forget;
  navigateTab = state.navigated;
  changedTab = state.changed;
  observeEvent = state.notify;
  clearCaptureImage = state.clearImage;
  connection.onMessage.addListener(async request => {
    let response;
    const id = typeof request?.id === "string" && request.id.length <= 36 ? request.id : "";
    let result;
    try {
      result = request?.v === version ? await operation(state, request) : { status: "protocol_mismatch" };
    } catch (_) {
      result = { status: request?.operation === "close" ? "cleanup_unconfirmed" : request?.operation === "front_open" ? "open_unconfirmed" :
        ["page", "tab"].includes(request?.operation) ? "action_unconfirmed" : "browser_error" };
    }
    response = withinBudget({ v: version, id, ...result }, request?.operation, request?.payload?.kind);
    try {
      if (port === connection) connection.postMessage(response);
    } finally {
      if (result.status === "uninstall_dispatched") {
        // The official self-uninstall can terminate the worker and host before
        // anything else can be reported. A sent marker is not proof of removal.
        try { await chrome.management.uninstallSelf({ showConfirmDialog: false }); }
        catch (_) { /* The native client reports the result as unverified. */ }
      }
    }
  });
  connection.onDisconnect.addListener(() => {
    // Reading lastError suppresses the warning when the host isn't installed yet.
    void chrome.runtime.lastError;
    if (port !== connection) return;
    state.clearImage();
    port = undefined;
    forgetTab = () => {};
    navigateTab = () => {};
    changedTab = () => {};
    observeEvent = () => {};
    clearCaptureImage = () => {};
    if (!uninstallDispatched) chrome.alarms.create(reconnect, { delayInMinutes: 0.5 });
  });
}

chrome.alarms.onAlarm.addListener(alarm => {
  if (alarm.name === reconnect) connect();
});
chrome.runtime.onStartup.addListener(connect);
chrome.runtime.onInstalled.addListener(connect);
chrome.permissions.onRemoved?.addListener(() => {
  observeEvent("permission");
  clearCaptureImage();
});
chrome.action.onClicked.addListener(() => {
  // Call directly from the toolbar gesture; a native request never opens a prompt.
  void chrome.permissions.request(captureGrant).catch(() => {});
});
connect();
