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

async function harness({windows = [], grant = true, privateAccess = false, ignoreDocumentTarget = false,
  getAll, nativeMessage, checkPrivate, pageResult, captureImage, operationChangedDocument = false,
  captureChangesSelection = false, redirect} = {}) {
  let scripts = 0, creates = 0, closes = 0, focuses = 0, captures = 0;
  let frameId = documentId;
  const updates = [];
  const events = () => ({addListener(callback) { this.fire = callback; }});
  const onRemoved = events(), onActivated = events(), onFocusChanged = events(), onCommitted = events();
  const profile = randomUUID();
  const window = () => windows[0];
  const browser = {
    runtime: {onMessage: listener, sendNativeMessage: (_app, message) => nativeMessage(message)},
    extension: {inIncognitoContext: false, isAllowedIncognitoAccess: checkPrivate || (async () => privateAccess)},
    permissions: {contains: async () => grant},
    storage: {local: {get: async () => ({enabledProfile: profile}), remove: async () => {}}},
    tabs: {onRemoved, onActivated, get: async id => window()?.tabs.find(tab => tab.id === id),
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
      captureVisibleTab: async () => {
        captures++;
        if (captureChangesSelection) onActivated.fire({tabId: 2});
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
        (pageResult?.(args[0]) ?? {status: "ok", final_url: args[0].expectedURL}) : {status: "ok", final_url: url, text: "fixture"};
      if (operationChangedDocument && args?.[0].kind) {
        frameId = randomUUID();
        onCommitted.fire({tabId: target.tabId, frameId: 0});
      }
      return [{documentId: currentFrame, frameId: 0, result}];
    }},
  };
  const context = vm.createContext({browser, crypto: {randomUUID}, URL, setTimeout() { return 1; }, clearTimeout() {}});
  vm.runInContext(readFileSync(join(__dirname, "../extension/shared/read-page.js"), "utf8"), context);
  vm.runInContext('if (typeof agentBrowserPageOperation !== "function") { globalThis.agentBrowserPageOperation = function agentBrowserPageOperation() {}; }', context);
  vm.runInContext(readFileSync(join(__dirname, "Web/background.js"), "utf8"), context);
  await vm.runInContext("ready", context);
  const execute = vm.runInContext("execute", context);
  return {call: (operation, payload) => execute({operation, payload, expires: Date.now() + 10000}),
    poll: () => vm.runInContext("poll(enabledProfile)", context),
    bound: (result, operation) => vm.runInContext("bound", context)({v: 2, id: "a".repeat(32), ...result}, operation),
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
  const request = {v: 2, id: "a".repeat(32), operation: "find", payload: {url}, expires: Date.now() + 10000};
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

test("capture does not activate tabs, crop, or truncate an oversized image", async () => {
  const png = "data:image/png;base64,AAAA";
  const browser = await harness({windows: windowWithTab(), captureImage: png});
  const ref = (await browser.call("list", {})).tabs[0].ref;
  const payload = {ref, expected_url: url};
  assert.equal((await browser.call("capture", {...payload, selector: "button"})).status, "action_unavailable");
  assert.equal(browser.counters().captures, 0);
  const captured = await browser.call("capture", payload);
  assert.equal(captured.image, png);
  const tooLarge = browser.bound({...captured, image: png + "A".repeat(65536)}, "capture");
  assert.equal(tooLarge.status, "response_too_large");
  assert.equal(tooLarge.image, undefined);
  assert.equal(browser.updates.length, 0);
  assert.equal(browser.counters().focuses, 0);
});

test("capture discards pixels if activation changes during the browser capture", async () => {
  const browser = await harness({windows: windowWithTab(), captureImage: "data:image/png;base64,AAAA", captureChangesSelection: true});
  const ref = (await browser.call("list", {})).tabs[0].ref;
  const result = await browser.call("capture", {ref, expected_url: url});
  assert.equal(result.status, "target_changed");
  assert.equal(result.image, undefined);
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
      request: {v: 2, id: "b".repeat(32), operation: "find", payload: {url}, expires: Date.now() + 10000}};
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
