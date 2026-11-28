"use strict";

const app = "com.ronigooja.agentbrowser.safaribackground";
const instance = crypto.randomUUID();
const origins = ["http://*/*", "https://*/*"];
const uuid = /^[\da-f]{8}(?:-[\da-f]{4}){3}-[\da-f]{12}$/i;
const requestID = /^[\da-f]{32}$/;
const limit = 65536;
const version = 3;
const imageLimit = 12 * 1024 * 1024;
const imageChunkSize = 48000;
const imageLifetime = 60000;
const imagePollInterval = 50;
const pngPrefix = "data:image/png;base64,";
const refPlaceholder = "0".repeat(73);
const pageFields = new Map([
  ["info", []], ["html", ["selector", "raw"]], ["snapshot", ["selector", "offset"]], ["extract", ["selector", "offset"]],
  ["wait", ["selector", "condition", "text"]], ["click", ["selector", "elementRef"]],
  ["scroll", ["selector", "elementRef", "direction", "pixels"]], ["fill", ["selector", "elementRef", "text"]],
  ["select", ["selector", "elementRef", "value"]], ["press", ["selector", "elementRef", "key"]],
  ["submit", ["selector", "elementRef"]],
]);
const pageWrites = new Set(["click", "scroll", "fill", "select", "press", "submit"]);
const refs = new Map(), tabs = new Map();
const interval = 1000;
let enabledProfile;
let timer;
let polling = false;
let selectionEpoch = 0;
let lastActivation = 0;
let readPinQualified = false;
let bufferedImage;

const native = (phase, extra = {}) => browser.runtime.sendNativeMessage(app, {v: version, phase, instance, ...extra});
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
  const pinned = () => ref(data.ref) && !!httpURL(data.expected_url);
  if (operation === "status" || operation === "list") return only();
  if (operation === "find" || operation === "open" || operation === "front_open") return only("url") && !!httpURL(data.url);
  if (operation === "tab") return pinned() && only("ref", "expected_url", "kind", "url") &&
    ["navigate", "activate", "close"].includes(data.kind) && (data.kind === "navigate" ? !!httpURL(data.url) : !("url" in data));
  if (operation === "capture") return pinned() && only("ref", "expected_url", "selector") &&
    (!("selector" in data) || selector(data.selector));
  if (operation === "capture_chunk") return only("image_ref", "offset") &&
    typeof data.image_ref === "string" && data.image_ref.length === 32 && requestID.test(data.image_ref) &&
    isID(data.offset) && data.offset < imageLimit;
  if (operation === "page") {
    const fields = pageFields.get(data.kind);
    const sizes = {selector: 1000, elementRef: 256, text: 4000, key: 32, value: 1000, direction: 8, condition: 8};
    return !!fields && pinned() && only("ref", "expected_url", "kind", ...fields) && !("selector" in data && "elementRef" in data) &&
      Object.entries(sizes).every(([key, max]) => !(key in data) || (typeof data[key] === "string" &&
        data[key].length <= max && !data[key].includes("\0") && (["text", "value"].includes(key) || !!data[key].trim()))) &&
      (!("pixels" in data) || (Number.isSafeInteger(data.pixels) && Math.abs(data.pixels) <= 10000)) &&
      (!("offset" in data) || (Number.isSafeInteger(data.offset) && data.offset >= 0 && data.offset <= 10000)) &&
      (!("raw" in data) || typeof data.raw === "boolean");
  }
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
removed?.addListener(id => {
  const entry = tabs.get(id);
  if (entry) entry.removed = true;
  forget(id);
});
activated?.addListener(({tabId}) => {
  lastActivation++;
  const entry = tabs.get(tabId);
  if (entry) delete entry.ownership; // The user may have adopted this tab.
});
windowFocusChanged?.addListener(() => { lastActivation++; });
browser.tabs?.onReplaced?.addListener((_new, old) => forget(old));
browser.tabs?.onDetached?.addListener(forget);
browser.tabs?.onAttached?.addListener(forget);
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

const titleOf = tab => typeof tab.title === "string" ? tab.title.slice(0, 500) : "";

function describe(tab, url) {
  let entry = tabs.get(tab.id);
  if (entry && entry.window !== tab.windowId) { forget(tab.id); entry = undefined; }
  if (!entry) {
    if (tabs.size >= 256) return null;
    entry = {id: tab.id, window: tab.windowId, ref: `${instance}.${crypto.randomUUID()}`, navigation: 0};
    tabs.set(tab.id, entry);
    refs.set(entry.ref, entry);
  }
  return {ref: entry.ref, url: url.href, title: titleOf(tab)};
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
  const tab = window.tabs.find(tab => regular(tab) && tab.id === entry.id && tab.windowId === entry.window);
  return tab ? {...tab, windowFocused: window.focused,
    windowActiveCount: window.tabs.filter(item => item.active === true).length} : null;
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

function originAllowed(expected) {
  const target = httpURL(expected);
  return browser.permissions.contains({origins: [`${target.protocol}//${target.hostname}/*`]});
}

const readStatuses = new Set(["ok", "verification_required", "passive_challenge", "login_required", "page_loading",
  "content_not_ready", "invalid_wait_selector", "invalid_selector", "no_match", "no_content"]);

async function pinDocument({ref, expected_url}, expires) {
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
  if (await originAllowed(expected) !== true) return unavailable;
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
  const current = await currentTab(entry);
  const currentMismatch = boundary(current, expected);
  if (currentMismatch) return currentMismatch;
  const pin = await topDocument(entry.id);
  if (selection !== selectionEpoch || refs.get(ref) !== entry || entry.navigation !== navigation || !trustedDocument(pin, expected) ||
      pin.documentId !== frame.documentId) return changed();
  if (expired(expires)) return {status: "timeout"};
  return {entry, tab: current, expected, frame, selection, navigation, expires, script};
}

const stillPinned = ({entry, selection, navigation, expires}) => selection === selectionEpoch &&
  refs.get(entry.ref) === entry && entry.navigation === navigation && !expired(expires);

async function checkPin(pin) {
  const {entry, expected, frame, expires} = pin;
  if (await setupStatus() !== "ok" || await originAllowed(expected) !== true || pin.selection !== selectionEpoch) {
    return {status: "read_unavailable", final_url: expected};
  }
  const tab = await currentTab(entry);
  const mismatch = boundary(tab, expected);
  if (mismatch) return mismatch;
  const post = await topDocument(entry.id);
  if (expired(expires)) return {status: "timeout"};
  if (!stillPinned(pin) || !trustedDocument(post, expected) ||
      post.documentId !== frame.documentId) return changed(httpURL(post?.url));
  return {tab};
}

function pinnedResult(results, frame) {
  return Array.isArray(results) && results.length === 1 && results[0]?.documentId === frame.documentId &&
    results[0].frameId === 0 ? results[0].result : null;
}

const pageStatuses = new Set([...readStatuses, "target_changed", "stale_element", "action_unavailable", "action_unconfirmed"]);
const actionUnconfirmed = {status: "action_unconfirmed"};

async function page(data, expires) {
  const {ref, expected_url, ...options} = data;
  const pin = await pinDocument(data, expires);
  if (pin.status) return pin;
  return runPage(pin, options);
}

async function runPage(pin, options) {
  const {script, frame, expected, entry} = pin;
  const unavailable = {status: "read_unavailable", final_url: expected};
  if (typeof agentBrowserPageOperation !== "function") return unavailable;
  if (!stillPinned(pin)) return changed();
  const mutation = pageWrites.has(options.kind);
  // An interaction might adopt a previously owned tab even when its outcome cannot be proved.
  if (mutation) delete entry.ownership;
  try {
    const results = await script(frame.documentId, agentBrowserPageOperation, [{...options, expectedURL: expected}]);
    const post = await checkPin(pin);
    if (post.status) return mutation ? actionUnconfirmed : post;
    const result = pinnedResult(results, frame);
    if (!pageStatuses.has(result?.status)) return mutation ? actionUnconfirmed : unavailable;
    if (result.status === "target_changed" || result.final_url !== expected) {
      return mutation ? actionUnconfirmed : changed(httpURL(result.final_url));
    }
    return result;
  } catch (_) { return mutation ? actionUnconfirmed : unavailable; }
}

const wireSize = value => JSON.stringify(value).replace(/[\/<>&\x7f-\uffff]/g, "\\u0000").length;
// Reserve room for the native envelope; v3 writes the ASCII PNG without escaping slashes.
const inlineImageFits = (image, metadata) => image.length <= imageChunkSize && wireSize(metadata) + image.length + 128 <= limit;

async function listTabs(expires) {
  const selection = selectionEpoch;
  const windows = await inventory();
  if (!windows) return {status: "window_snapshot_unavailable"};
  if (await setupStatus() !== "ok" || selection !== selectionEpoch) return {status: "disconnected"};
  if (expired(expires)) return {status: "timeout"};
  const result = [], seen = new Set();
  let truncated = false;
  for (const window of windows) {
    for (const tab of window.tabs) {
      const url = httpURL(tab.url);
      if (!regular(tab) || tab.windowId !== window.id || !url || tab.pendingUrl !== undefined ||
          tab.status === "loading" || tab.discarded === true || typeof tab.active !== "boolean" || seen.has(tab.id)) continue;
      seen.add(tab.id);
      if (result.length === 32) { truncated = true; continue; }
      const item = {ref: refPlaceholder, url: url.href, title: titleOf(tab), active: tab.active, window_id: window.id};
      if (wireSize([...result, item]) > limit - 256) { truncated = true; continue; }
      const description = describe(tab, url);
      if (!description) { truncated = true; continue; }
      item.ref = description.ref;
      result.push(item);
    }
  }
  return {status: "ok", tabs: result, ...(truncated ? {truncated: true} : {})};
}

async function tabOperation(data, expires) {
  const pin = await pinDocument(data, expires);
  if (pin.status) return pin;
  const gate = await runPage(pin, {kind: "info"});
  if (gate.status !== "ok") return gate;
  const before = await checkPin(pin);
  if (before.status) return before;
  if (!stillPinned(pin)) return changed();
  const {entry, expected} = pin;
  const {kind} = data;
  const activation = lastActivation;
  if (kind === "navigate" && (!activated?.addListener || !windowFocusChanged?.addListener ||
      typeof before.tab.active !== "boolean" || typeof before.tab.windowFocused !== "boolean")) return {status: "action_unavailable"};
  delete entry.ownership;
  try {
    if (kind === "navigate") {
      // The API has no compare-and-update. Keep the pin adjacent to a URL-only
      // update, and do not request tab selection or foreground activation.
      const target = httpURL(data.url).href;
      await browser.tabs.update(entry.id, {url: target});
      while (!expired(expires) && selectionEpoch === pin.selection && refs.get(entry.ref) === entry && lastActivation === activation) {
        const tab = await currentTab(entry);
        if (!tab || tab.active !== before.tab.active || tab.windowFocused !== before.tab.windowFocused ||
            (observed(tab)?.href !== target && observed(tab)?.href !== expected)) return actionUnconfirmed;
        if (!boundary(tab, target)) {
          const destination = await pinDocument({ref: entry.ref, expected_url: target}, expires);
          if (!destination.status && lastActivation === activation && stillPinned(destination) &&
              (expected !== target || destination.frame.documentId !== pin.frame.documentId) &&
              destination.tab.active === before.tab.active && destination.tab.windowFocused === before.tab.windowFocused) {
            return {status: "ok", tab: describe(destination.tab, httpURL(target))};
          }
        }
        await new Promise(resolve => setTimeout(resolve, 100));
      }
      return actionUnconfirmed;
    }
    if (kind === "activate") {
      await browser.tabs.update(entry.id, {active: true});
      const selected = await checkPin(pin);
      if (selected.tab?.active !== true || !stillPinned(pin)) return actionUnconfirmed;
      await browser.windows.update(entry.window, {focused: true});
      const after = await checkPin(pin);
      if (after.tab?.windowFocused !== true || after.tab.active !== true || !stillPinned(pin)) return actionUnconfirmed;
      return {status: "ok", tab: describe(after.tab, httpURL(expected))};
    }
    await browser.tabs.remove(entry.id);
    if (pin.selection !== selectionEpoch || expired(expires) || entry.navigation !== pin.navigation) return actionUnconfirmed;
    // A removal event provides positive evidence even if closing the last tab
    // also closes the containing regular window.
    if (!entry.removed) return actionUnconfirmed;
    return {status: "ok"};
  } catch (_) { return actionUnconfirmed; }
}

const cropFields = ["x", "y", "width", "height", "viewport_width", "viewport_height"];
function captureCrop(crop) {
  if (!crop || typeof crop !== "object" || Array.isArray(crop) ||
      !cropFields.every(key => Number.isFinite(crop[key]) && crop[key] >= 0 && crop[key] <= 65536)) return null;
  const {x, y, width, height, viewport_width, viewport_height} = crop;
  if (width <= 0 || height <= 0 || x + width > viewport_width || y + height > viewport_height) return null;
  return {x, y, width, height, viewport_width, viewport_height};
}

const captureFocused = tab => tab.active === true && tab.windowFocused === true && tab.windowActiveCount === 1;

async function capture(data, expires) {
  const {selector} = data;
  if (!browser.tabs?.captureVisibleTab || !activated?.addListener || !windowFocusChanged?.addListener ||
      !browser.tabs?.onUpdated?.addListener || !browser.tabs?.onReplaced?.addListener ||
      !browser.tabs?.onDetached?.addListener || !browser.tabs?.onAttached?.addListener) return {status: "action_unavailable"};
  const activation = lastActivation;
  const pin = await pinDocument(data, expires);
  if (pin.status) return pin;
  const stale = tab => {
    if (expired(expires)) return {status: "timeout"};
    if (lastActivation !== activation || !stillPinned(pin) || (tab && !captureFocused(tab))) return changed();
    return null;
  };
  if (!captureFocused(pin.tab)) return {status: "action_unavailable"};
  const initialMismatch = stale();
  if (initialMismatch) return initialMismatch;
  const options = {kind: "capture_geometry", ...(selector === undefined ? {} : {selector})};
  const gate = await runPage(pin, options);
  const gateMismatch = stale();
  if (gateMismatch) return gateMismatch;
  if (gate.status !== "ok") return gate;
  const crop = captureCrop(gate.crop);
  if (!crop) return {status: "read_unavailable"};
  try {
    const before = await checkPin(pin);
    if (before.status) return before;
    const beforeMismatch = stale(before.tab);
    if (beforeMismatch) return beforeMismatch;
    bufferedImage = undefined; // Keep no prior buffer while Safari allocates the next viewport PNG.
    const image = await browser.tabs.captureVisibleTab(pin.entry.window, {format: "png"});
    const after = await checkPin(pin);
    if (after.status) return after;
    const afterMismatch = stale(after.tab);
    if (afterMismatch) return afterMismatch;
    const finalGate = await runPage(pin, options);
    const finalGateMismatch = stale();
    if (finalGateMismatch) return finalGateMismatch;
    if (finalGate.status !== "ok") {
      // Never publish the first capture if the post-gate detects a challenge or changed geometry.
      return ["no_match", "no_content", "content_not_ready"].includes(finalGate.status) ? changed() : finalGate;
    }
    const finalCrop = captureCrop(finalGate.crop);
    if (!finalCrop || !cropFields.every(key => crop[key] === finalCrop[key])) return changed();
    const final = await checkPin(pin);
    if (final.status) return final;
    const finalMismatch = stale(final.tab);
    if (finalMismatch) return finalMismatch;
    if (typeof image !== "string") return {status: "read_unavailable"};
    if (image.length > imageLimit) return {status: "response_too_large"};
    if ((image.length - pngPrefix.length) % 4 !== 0 ||
        !/^data:image\/png;base64,[A-Za-z0-9+/]+={0,2}(?![\s\S])/.test(image)) return {status: "read_unavailable"};
    const result = {status: "ok", final_url: pin.expected, ...(selector === undefined ? {} : {crop})};
    if (inlineImageFits(image, result)) return {...result, image};
    const image_ref = crypto.randomUUID().replace(/-/g, "");
    bufferedImage = {ref: image_ref, profile: enabledProfile, image, offset: 0, expires: Date.now() + imageLifetime};
    return {...result, image_ref, image_length: image.length};
  } catch (_) { return {status: "read_unavailable"}; }
}

function liveImage() {
  if (bufferedImage && (bufferedImage.profile !== enabledProfile || expired(bufferedImage.expires))) bufferedImage = undefined;
  return bufferedImage;
}

function captureChunk({image_ref, offset}) {
  const buffer = liveImage();
  if (!buffer || buffer.ref !== image_ref || buffer.offset !== offset) return {status: "read_unavailable"};
  const next_offset = Math.min(offset + imageChunkSize, buffer.image.length);
  const data = buffer.image.slice(offset, next_offset);
  buffer.offset = next_offset;
  if (next_offset === buffer.image.length) bufferedImage = undefined;
  return {status: "ok", data, next_offset};
}

async function read(data, expires) {
  const {selector, wait_for_selector, include_links} = data;
  const pin = await pinDocument(data, expires);
  if (pin.status) return pin;
  const {script, frame, expected} = pin;
  const results = await script(frame.documentId, readPage, [{expectedURL: expected,
    ...(selector !== undefined ? {selector} : {}), ...(wait_for_selector !== undefined ? {waitForSelector: wait_for_selector} : {}),
    includeLinks: include_links === true}]).catch(() => null);
  const post = await checkPin(pin);
  if (post.status) return post;
  const result = pinnedResult(results, frame);
  if (!result) return {status: "read_unavailable", final_url: expected};
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

async function frontOpen({url}, expires) {
  const selection = selectionEpoch;
  const target = httpURL(url);
  const windows = await inventory();
  if (!windows) return {status: "window_snapshot_unavailable"};
  if (!windows.length) return {status: "no_regular_window"};
  const matching = windows.flatMap(window => window.tabs).filter(tab => observed(tab)?.href === target.href);
  if (matching.length > 1) return {status: "tab_ambiguous"};
  if (matching.length === 1) {
    if (await setupStatus() !== "ok" || selection !== selectionEpoch) return {status: "disconnected"};
    if (expired(expires)) return {status: "timeout"};
    const tab = describe(matching[0], target);
    return tab ? {status: "ok", tab, created: false} : {status: "window_snapshot_unavailable"};
  }
  const focused = windows.filter(window => window.focused === true);
  const window = windows.length === 1 ? windows[0] : focused.length === 1 ? focused[0] : null;
  if (!window || !browser.windows?.update || !removed?.addListener || tabs.size >= 256) return {status: "window_snapshot_unavailable"};
  if (await setupStatus() !== "ok" || selection !== selectionEpoch) return {status: "disconnected"};
  if (expired(expires)) return {status: "timeout"};
  try {
    const tab = await browser.tabs.create({windowId: window.id, active: true, url: target.href});
    if (selection !== selectionEpoch || expired(expires) || !regular(tab) || tab.windowId !== window.id ||
        tab.active !== true || window.tabs.some(previous => previous.id === tab.id) || tabs.has(tab.id)) return actionUnconfirmed;
    const description = describe(tab, observed(tab) ?? target);
    const entry = refs.get(description?.ref);
    if (!entry) return actionUnconfirmed;
    const created = await currentTab(entry);
    if (selection !== selectionEpoch || expired(expires) || created?.active !== true || observed(created)?.href !== target.href) return actionUnconfirmed;
    await browser.windows.update(window.id, {focused: true});
    const after = await currentTab(entry);
    if (await setupStatus() !== "ok" || selection !== selectionEpoch || refs.get(entry.ref) !== entry || expired(expires) ||
        after?.active !== true || after.windowFocused !== true || observed(after)?.href !== target.href) return actionUnconfirmed;
    return {status: "ok", tab: describe(after, target), created: true};
  } catch (_) { return actionUnconfirmed; }
}

function uncertainStatus(operation, payload) {
  if (operation === "open") return "open_unconfirmed";
  if (operation === "close") return "cleanup_unconfirmed";
  if (operation === "tab" || operation === "front_open" || (operation === "page" && pageWrites.has(payload?.kind))) return "action_unconfirmed";
  return null;
}

async function execute(request) {
  const {operation, payload, expires} = request;
  if (!validPayload(operation, payload) || !Number.isSafeInteger(expires)) return {status: "invalid_request"};
  if (expired(expires)) return {status: "timeout"};
  const selection = selectionEpoch;
  const status = await setupStatus();
  if (selection !== selectionEpoch) return {status: "disconnected"};
  if (status !== "ok") { bufferedImage = undefined; return {status}; }
  if (expired(expires)) return {status: "timeout"};
  if (operation === "status") return {status: "ok"};
  if (operation === "list") return listTabs(expires);
  if (operation === "page") return page(payload, expires);
  if (operation === "tab") return tabOperation(payload, expires);
  if (operation === "capture") return capture(payload, expires);
  if (operation === "capture_chunk") return captureChunk(payload);
  if (operation === "front_open") return frontOpen(payload, expires);
  if (operation === "read") return read(payload, expires);
  if (operation === "close") return closeOwned(payload, expires);
  return findOpen(operation, payload, expires);
}

function bound(answer, operation) {
  // Swift's v3 framing does not escape slashes; PNG/base64 ASCII needs no JSON escaping.
  if (operation === "capture_chunk" && typeof answer.data === "string" && answer.data.length <= imageChunkSize) return answer;
  if (operation === "capture" && typeof answer.image === "string") {
    const {image, v, id, ...result} = answer;
    if (inlineImageFits(image, result)) return answer;
  }
  const size = () => wireSize(answer);
  if (size() <= limit) return answer;
  if (typeof answer.text === "string") {
    answer.truncated = true;
    while (answer.links?.length && size() > limit) answer.links.pop();
    while (answer.text.length && size() > limit) answer.text = answer.text.slice(0, Math.floor(answer.text.length * 0.75));
    if (answer.text && size() <= limit) return answer;
  }
  return {v: version, id: answer.id, status: (operation === "open" || operation === "front_open") && answer.created ?
    uncertainStatus(operation) : "response_too_large"};
}

async function poll(profile) {
  const selection = selectionEpoch;
  if (browser.extension?.inIncognitoContext !== false || await browser.extension?.isAllowedIncognitoAccess?.() !== false) {
    bufferedImage = undefined;
    return;
  }
  if (profile !== enabledProfile) return;
  const grant = await native("pull", {selectedProfile: profile});
  const request = grant?.request;
  if (grant?.status !== "claimed" || grant.profile?.toLowerCase() !== profile || grant.instance?.toLowerCase() !== instance ||
      request?.v !== version || typeof request.id !== "string" || !requestID.test(request.id)) return;
  if (selection !== selectionEpoch) return;
  let result;
  try { result = await execute(request); }
  catch (_) { result = {status: uncertainStatus(request.operation, request.payload) ?? "browser_error"}; }
  if (selection !== selectionEpoch) result = {status: uncertainStatus(request.operation, request.payload) ?? "disconnected"};
  await native("result", {selectedProfile: profile, id: request.id,
    answer: bound({v: version, id: request.id, ...result}, request.operation)});
}

function schedule(delay = liveImage() ? imagePollInterval : interval) {
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
  const selected = typeof profile === "string" && uuid.test(profile) ? profile.toLowerCase() : undefined;
  if (enabledProfile !== selected) {
    selectionEpoch++; readPinQualified = false; bufferedImage = undefined; refs.clear(); tabs.clear();
  }
  enabledProfile = selected;
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
