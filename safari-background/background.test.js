"use strict";

const {readFileSync} = require("node:fs");
const {join} = require("node:path");
const {randomUUID} = require("node:crypto");
const vm = require("node:vm");
const assert = require("node:assert/strict");
const {test} = require("node:test");

const url = "https://example.com/";
const documentId = randomUUID();
const listener = {addListener() {}};
const existing = {id: 1, windowId: 7, incognito: false, active: true, url};
const viewportCrop = {x: 0, y: 0, width: 800, height: 600, viewport_width: 800, viewport_height: 600};

async function harness({windows = [], grant = true, privateAccess = false, ignoreDocumentTarget = false,
  getAll, nativeMessage, checkPrivate, pageResult, captureImage, operationChangedDocument = false,
  captureEvent, redirect} = {}) {
  let scripts = 0, creates = 0, closes = 0, focuses = 0, captures = 0;
  let frameId = documentId;
  let now = Date.now();
  const updates = [];
  const events = () => ({addListener(callback) { this.fire = callback; }});
  const onRemoved = events(), onActivated = events(), onFocusChanged = events(), onCommitted = events();
  const onUpdated = events(), onReplaced = events(), onDetached = events(), onAttached = events();
  const profile = randomUUID();
  const window = () => windows[0];
  const browser = {
    runtime: {onMessage: listener, sendNativeMessage: (_app, message) => nativeMessage(message)},
    extension: {inIncognitoContext: false, isAllowedIncognitoAccess: checkPrivate || (async () => privateAccess)},
    permissions: {contains: async () => grant},
    storage: {local: {get: async () => ({enabledProfile: profile}), remove: async () => {}}},
    tabs: {onRemoved, onActivated, onUpdated, onReplaced, onDetached, onAttached,
      get: async id => window()?.tabs.find(tab => tab.id === id),
      create: async ({url: target, active}) => {
        creates++;
        const tab = {...existing, id: 2, active, url: target};
        // Safari returns value snapshots; a later create cannot mutate the prior one.
        windows = [{...window(), tabs: [...window().tabs.map(row => active ? {...row, active: false} : row), tab]}];
        if (active) onActivated.fire({tabId: tab.id});
        return tab;
      },
      update: async (id, properties) => {
        updates.push(properties);
        const changed = properties.url && redirect ? {...properties, url: redirect} : properties;
        windows = [{...window(), tabs: window().tabs.map(tab => tab.id === id ? {...tab, ...changed} :
          changed.active ? {...tab, active: false} : tab)}];
        if (properties.url) { frameId = randomUUID(); onCommitted.fire({tabId: id, frameId: 0}); }
        if (properties.active) onActivated.fire({tabId: id});
      },
      remove: async id => { closes++; onRemoved.fire(id); },
      captureVisibleTab: async (windowId, options) => {
        assert.equal(windowId, window().id);
        assert.equal(options.format, "png");
        captures++;
        if (captureEvent === "activation") onActivated.fire({tabId: 2});
        if (captureEvent === "focus") onFocusChanged.fire(-1);
        if (captureEvent === "update") onUpdated.fire(1, {url: `${url}#transient`});
        if (captureEvent === "attach") onAttached.fire(1);
        if (captureEvent === "document") frameId = randomUUID();
        return captureImage;
      }},
    windows: {getAll: getAll || (async () => windows), get: async () => window(), onFocusChanged,
      update: async () => { focuses++; windows = [{...window(), focused: true}]; onFocusChanged.fire(); }},
    webNavigation: {onBeforeNavigate: listener, onCommitted,
      getFrame: async ({tabId}) => ({url: window()?.tabs.find(tab => tab.id === tabId)?.url, documentId: frameId,
        parentFrameId: -1, errorOccurred: false})},
    scripting: {executeScript: async ({target, func, args}) => {
      scripts++;
      if (target.documentIds[0] !== frameId && !ignoreDocumentTarget) return [];
      const currentFrame = frameId;
      const result = func.name === "noDataMarker" ? true : args?.[0].kind ?
        (pageResult?.(args[0]) ?? {status: "ok", final_url: args[0].expectedURL,
          ...(args[0].kind === "capture_geometry" ? {crop: viewportCrop} : {})}) : {status: "ok", final_url: url, text: "fixture"};
      if (operationChangedDocument && args?.[0].kind) {
        frameId = randomUUID();
        onCommitted.fire({tabId: target.tabId, frameId: 0});
      }
      return [{documentId: currentFrame, frameId: 0, result}];
    }},
  };
  const delays = [];
  const context = vm.createContext({browser, crypto: {randomUUID}, URL, Date: {now: () => now},
    setTimeout(_callback, delay) { delays.push(delay); return 1; }, clearTimeout() {}});
  vm.runInContext(readFileSync(join(__dirname, "../extension/shared/read-page.js"), "utf8"), context);
  vm.runInContext('if (typeof agentBrowserPageOperation !== "function") { globalThis.agentBrowserPageOperation = function agentBrowserPageOperation() {}; }', context);
  vm.runInContext(readFileSync(join(__dirname, "Web/background.js"), "utf8"), context);
  await vm.runInContext("ready", context);
  const execute = vm.runInContext("execute", context);
  return {call: (operation, payload) => execute({operation, payload, expires: now + 10000}),
    poll: () => vm.runInContext("poll(enabledProfile)", context),
    bound: (result, operation) => vm.runInContext("bound", context)({v: 3, id: "a".repeat(32), ...result}, operation),
    nextPollDelay: () => { vm.runInContext("timer = undefined; schedule()", context); return delays.at(-1); },
    advance: ms => { now += ms; },
    changeProfile: () => vm.runInContext("configure(crypto.randomUUID())", context),
    disable: () => vm.runInContext('setup("disable")', context),
    profile, instance: vm.runInContext("instance", context),
    counters: () => ({scripts, creates, closes, focuses, captures}), updates};
}

const windowWithTab = () => [{id: 7, type: "normal", incognito: false, focused: true, tabs: [{...existing}]}];

test("blocks absent broad grant and unverified private access", async () => {
  const missing = await harness({grant: false});
  assert.equal((await missing.call("open", {url})).status, "site_access_required");
  assert.equal(missing.counters().creates, 0);
  const privateBrowser = await harness({privateAccess: true});
  assert.equal((await privateBrowser.call("status", {})).status, "private_access_enabled_or_unverified");
});

test("a document target ignored by Safari stops extraction before the data probe", async () => {
  const browser = await harness({windows: windowWithTab(), ignoreDocumentTarget: true});
  const found = await browser.call("find", {url});
  const read = await browser.call("read", {ref: found.tabs[0].ref, expected_url: url});
  assert.equal(read.status, "read_unavailable");
  assert.equal(browser.counters().scripts, 1);
});

test("a matching guarded document can be read", async () => {
  const browser = await harness({windows: windowWithTab()});
  const found = await browser.call("find", {url});
  const read = await browser.call("read", {ref: found.tabs[0].ref, expected_url: url});
  assert.equal(read.status, "ok");
  assert.equal(read.text, "fixture");
  assert.equal(browser.counters().scripts, 3);
});

test("does not create a window and only closes a newly owned inactive tab once", async () => {
  const empty = await harness();
  assert.equal((await empty.call("open", {url})).status, "no_regular_window");
  assert.equal(empty.counters().creates, 0);
  const browser = await harness({windows: windowWithTab()});
  const reused = await browser.call("open", {url});
  assert.equal(reused.created, false);
  assert.equal((await browser.call("close", {ref: reused.tab.ref, ownership: randomUUID()})).status, "cleanup_unconfirmed");
  const opened = await browser.call("open", {url: "https://example.org/"});
  assert.equal(opened.created, true);
  const payload = {ref: opened.tab.ref, ownership: opened.ownership};
  assert.equal((await browser.call("close", payload)).status, "closed");
  assert.equal((await browser.call("close", payload)).status, "cleanup_tab_not_found");
  assert.equal(browser.counters().closes, 1);
});

// Synthetic JS routing checks: native leases and live Safari profile isolation are separate gates.
test("rejects grants routed to another profile or instance", async () => {
  const messages = [];
  let supplied;
  const browser = await harness({windows: windowWithTab(), nativeMessage: async message => {
    messages.push(message);
    return supplied;
  }});
  const request = {v: 3, id: "a".repeat(32), operation: "find", payload: {url}, expires: Date.now() + 10000};
  for (const [profile, instance] of [[randomUUID(), browser.instance], [browser.profile, randomUUID()]]) {
    supplied = {status: "claimed", profile, instance, request};
    await browser.poll();
  }
  assert.equal(messages.filter(message => message.phase === "result").length, 0);
  assert.equal(browser.counters().scripts, 0);
  supplied = {status: "claimed", profile: browser.profile, instance: browser.instance, request};
  await browser.poll();
  supplied = {...supplied, request: {...request, v: 1}};
  await browser.poll();
  const results = messages.filter(message => message.phase === "result");
  assert.equal(results.length, 1);
  assert.equal(results[0].answer.status, "ok");
});

test("lists up to 32 regular HTTP(S) tabs and never includes private window or tab data", async () => {
  const windows = windowWithTab();
  windows[0].tabs.push({ ...existing, id: 40, incognito: true, url: "https://private.example/" },
    { ...existing, id: 41, url: "file:///local" }, { ...existing, id: 42, windowId: 8 },
    ...Array.from({length: 35}, (_, i) => ({...existing, id: i + 50, active: false})));
  windows.push({...windows[0], id: 8, incognito: true});
  const browser = await harness({windows});
  const result = await browser.call("list", {});
  assert.equal(result.status, "ok");
  assert.equal(result.truncated, true);
  assert.equal(result.tabs.length, 32);
  assert.equal(result.tabs[0].active, true);
  assert.equal(result.tabs[0].window_id, 7);
  assert.ok(result.tabs.every(tab => tab.url === url));
  assert.equal((await browser.call("list", {ref: result.tabs[0].ref})).status, "invalid_request");
});

test("passes only bounded declarative page operations to the packaged probe", async () => {
  let received;
  const browser = await harness({windows: windowWithTab(), pageResult: data => {
    received = data;
    return {status: "login_required", final_url: data.expectedURL};
  }});
  const ref = (await browser.call("list", {})).tabs[0].ref;
  const payload = {ref, expected_url: url, kind: "click", selector: "button"};
  assert.equal((await browser.call("page", {...payload, code: "arbitrary"})).status, "invalid_request");
  assert.equal((await browser.call("page", {...payload, kind: "evaluate"})).status, "invalid_request");
  assert.equal((await browser.call("page", {...payload, expected_url: "https://example.com/?other"})).status, "target_changed");
  assert.equal(browser.counters().scripts, 0);
  assert.equal((await browser.call("page", payload)).status, "login_required");
  assert.equal(received.selector, "button");
  assert.equal(received.expectedURL, url);
  assert.equal(received.ref, undefined);
});

test("discards a replaced document's result, and does not claim a mutation failed", async () => {
  for (const [kind, status] of [["info", "target_changed"], ["click", "action_unconfirmed"]]) {
    const browser = await harness({windows: windowWithTab(), operationChangedDocument: true});
    const ref = (await browser.call("find", {url})).tabs[0].ref;
    assert.equal((await browser.call("page", {ref, expected_url: url, kind})).status, status);
  }
});

test("navigation sends URL only, and explicit activate and close require the current exact URL", async () => {
  const browser = await harness({windows: windowWithTab()});
  const ref = (await browser.call("list", {})).tabs[0].ref;
  const destination = "https://example.com/next";
  const navigate = await browser.call("tab", {ref, expected_url: url, kind: "navigate", url: destination});
  assert.equal(navigate.status, "ok");
  assert.equal(navigate.tab.ref, ref);
  assert.equal(navigate.tab.url, destination);
  assert.deepEqual(JSON.parse(JSON.stringify(browser.updates)), [{url: destination}]);
  assert.equal(browser.counters().focuses, 0);
  assert.equal((await browser.call("tab", {ref, expected_url: url, kind: "close"})).status, "target_changed");
  assert.equal(browser.counters().closes, 0);
  assert.equal((await browser.call("tab", {ref, expected_url: destination, kind: "activate"})).status, "ok");
  assert.equal(browser.counters().focuses, 1);
  assert.equal((await browser.call("tab", {ref, expected_url: destination, kind: "close"})).status, "ok");
  assert.equal(browser.counters().closes, 1);
});

test("foreground open is explicit, unowned and never creates a window", async () => {
  const empty = await harness();
  assert.equal((await empty.call("front_open", {url})).status, "no_regular_window");
  const browser = await harness({windows: windowWithTab()});
  const reused = await browser.call("front_open", {url});
  assert.equal(reused.created, false);
  assert.equal(browser.counters().focuses, 0);
  assert.equal(browser.counters().creates, 0);
  const result = await browser.call("front_open", {url: "https://example.org/"});
  assert.equal(result.status, "ok");
  assert.equal(result.created, true);
  assert.equal(result.ownership, undefined);
  assert.equal(browser.counters().focuses, 1);
});

test("navigation never returns a redirected target capability", async () => {
  const browser = await harness({windows: windowWithTab(), redirect: "https://example.org/"});
  const ref = (await browser.call("list", {})).tabs[0].ref;
  const result = await browser.call("tab", {ref, expected_url: url, kind: "navigate", url: "https://example.com/redirect"});
  assert.equal(result.status, "action_unconfirmed");
  assert.equal(result.tab, undefined);
  assert.equal(browser.updates.length, 1);
});

test("capture returns stable crop geometry and never activates or focuses", async () => {
  const png = "data:image/png;base64,AAAA";
  const crop = {x: 0.5, y: 20, width: 200, height: 49.5, viewport_width: 800, viewport_height: 600};
  const calls = [];
  const browser = await harness({windows: windowWithTab(), captureImage: png, pageResult: data => {
    calls.push(data);
    return {status: "ok", final_url: data.expectedURL, crop};
  }});
  const ref = (await browser.call("list", {})).tabs[0].ref;
  const payload = {ref, expected_url: url};
  const captured = await browser.call("capture", payload);
  assert.equal(captured.image, png);
  assert.equal(captured.crop, undefined);
  const selected = await browser.call("capture", {...payload, selector: "button"});
  assert.equal(selected.image, png);
  assert.deepEqual({...selected.crop}, crop);
  assert.deepEqual(calls.map(data => [data.kind, data.selector, data.expectedURL]),
    [["capture_geometry", undefined, url], ["capture_geometry", undefined, url],
      ["capture_geometry", "button", url], ["capture_geometry", "button", url]]);
  assert.equal(browser.updates.length, 0);
  assert.equal(browser.counters().focuses, 0);
});

test("capture refuses inactive, unfocused or ambiguous targets before retrieving pixels", async () => {
  for (const change of [windows => { windows[0].focused = false; }, windows => { windows[0].tabs[0].active = false; },
    windows => { windows[0].tabs.push({...existing, id: 2}); }]) {
    const windows = windowWithTab();
    change(windows);
    const browser = await harness({windows});
    const ref = (await browser.call("list", {})).tabs[0].ref;
    assert.equal((await browser.call("capture", {ref, expected_url: url})).status, "action_unavailable");
    assert.equal(browser.counters().captures, 0);
  }
});

test("capture discards pixels on tab selection, focus, navigation, attach, or document changes", async () => {
  for (const captureEvent of ["activation", "focus", "update", "attach", "document"]) {
    const browser = await harness({windows: windowWithTab(), captureImage: "data:image/png;base64,AAAA", captureEvent});
    const ref = (await browser.call("list", {})).tabs[0].ref;
    const result = await browser.call("capture", {ref, expected_url: url});
    assert.equal(result.status, "target_changed", captureEvent);
    assert.equal(result.image, undefined);
    assert.equal(result.image_ref, undefined);
  }
});

test("capture never returns pixels if selector geometry changes or either page gate blocks", async () => {
  for (const blocking of ["geometry", "verification_required", "login_required", "no_match", "invalid_selector"]) {
    const beforeGate = ["no_match", "invalid_selector"].includes(blocking);
    let probes = 0;
    const browser = await harness({windows: windowWithTab(), captureImage: "data:image/png;base64,AAAA", pageResult: data => {
      probes++;
      const status = beforeGate || (probes === 2 && blocking !== "geometry") ? blocking : "ok";
      return {status, final_url: data.expectedURL, crop: {x: probes, y: 20, width: 200, height: 100,
        viewport_width: 800, viewport_height: 600}};
    }});
    const ref = (await browser.call("list", {})).tabs[0].ref;
    const result = await browser.call("capture", {ref, expected_url: url, selector: "button"});
    assert.equal(result.status, blocking === "geometry" ? "target_changed" : blocking);
    assert.equal(result.image, undefined);
    assert.equal(result.image_ref, undefined);
    assert.equal(browser.counters().captures, Number(!beforeGate));
  }
});

test("large capture splits ASCII in one-use ordered chunks and fast-polls while buffered", async () => {
  const png = "data:image/png;base64," + "/".repeat(96000);
  const browser = await harness({windows: windowWithTab(), captureImage: png});
  const ref = (await browser.call("list", {})).tabs[0].ref;
  const result = await browser.call("capture", {ref, expected_url: url});
  assert.equal(result.status, "ok");
  assert.equal(result.image, undefined);
  assert.equal(result.image_length, png.length);
  assert.match(result.image_ref, /^[\da-f]{32}$/);
  const image_ref = result.image_ref;
  const read = offset => browser.call("capture_chunk", {image_ref, offset});
  assert.equal(browser.nextPollDelay(), 50);
  assert.equal((await read(-1)).status, "invalid_request");
  assert.equal((await read(48000)).status, "read_unavailable");
  const chunks = [];
  for (let offset = 0; offset < result.image_length;) {
    const chunk = await read(offset);
    assert.equal(chunk.status, "ok");
    assert.equal(chunk.data.length, Math.min(48000, png.length - offset));
    assert.equal(chunk.next_offset, offset + chunk.data.length);
    assert.equal(browser.bound(chunk, "capture_chunk").status, "ok");
    assert.equal((await read(offset)).status, "read_unavailable");
    chunks.push(chunk.data);
    offset = chunk.next_offset;
  }
  assert.equal(chunks.join(""), png);
  assert.equal(browser.nextPollDelay(), 1000);
});

test("large captures expire, cannot cross profiles or instances, and use one memory slot", async () => {
  const options = {windows: windowWithTab(), captureImage: "data:image/png;base64," + "A".repeat(48000)};
  const browser = await harness(options);
  const ref = (await browser.call("list", {})).tabs[0].ref;
  const capture = () => browser.call("capture", {ref, expected_url: url});
  const original = await capture();
  const other = await harness(options);
  assert.equal((await other.call("capture_chunk", {image_ref: original.image_ref, offset: 0})).status, "read_unavailable");
  const replacement = await capture();
  assert.equal((await browser.call("capture_chunk", {image_ref: original.image_ref, offset: 0})).status, "read_unavailable");
  browser.advance(60000);
  assert.equal((await browser.call("capture_chunk", {image_ref: replacement.image_ref, offset: 0})).status, "read_unavailable");
  assert.equal(browser.nextPollDelay(), 1000);
  const last = await capture();
  browser.changeProfile();
  assert.equal((await browser.call("capture_chunk", {image_ref: last.image_ref, offset: 0})).status, "read_unavailable");
});

test("capture rejects malformed and over-limit data URLs", async () => {
  for (const [captureImage, status] of [["data:image/png;base64,AAA\n", "read_unavailable"],
    ["data:image/png;base64," + "A".repeat(12 * 1024 * 1024), "response_too_large"]]) {
    const browser = await harness({windows: windowWithTab(), captureImage});
    const ref = (await browser.call("list", {})).tabs[0].ref;
    const result = await browser.call("capture", {ref, expected_url: url});
    assert.equal(result.status, status);
    assert.equal(result.image, undefined);
    assert.equal(result.image_ref, undefined);
  }
});

test("disable during a claimed operation suppresses the result data", async () => {
  let release, entered;
  const blocked = new Promise(resolve => { release = resolve; });
  const started = new Promise(resolve => { entered = resolve; });
  const messages = [];
  let browser;
  browser = await harness({getAll: async () => { entered(); return blocked; }, nativeMessage: async message => {
    messages.push(message);
    if (message.phase === "pull") return {status: "claimed", profile: browser.profile, instance: browser.instance,
      request: {v: 3, id: "b".repeat(32), operation: "find", payload: {url}, expires: Date.now() + 10000}};
  }});
  const pending = browser.poll();
  await started;
  await browser.disable();
  release(windowWithTab());
  await pending;
  const result = messages.find(message => message.phase === "result");
  assert.equal(result.answer.status, "disconnected");
  assert.equal(result.answer.tabs, undefined);
  assert.equal(browser.counters().creates, 0);
});

test("disable during the initial permission check prevents dispatch", async () => {
  let release, entered, inventories = 0;
  const blocked = new Promise(resolve => { release = resolve; });
  const started = new Promise(resolve => { entered = resolve; });
  const browser = await harness({checkPrivate: async () => { entered(); return blocked; },
    getAll: async () => { inventories++; return windowWithTab(); }});
  const pending = browser.call("find", {url});
  await started;
  await browser.disable();
  release(false);
  assert.equal((await pending).status, "disconnected");
  assert.equal(inventories, 0);
});
