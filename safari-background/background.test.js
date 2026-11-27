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
  getAll, nativeMessage, checkPrivate} = {}) {
  let scripts = 0, creates = 0, closes = 0;
  const profile = randomUUID();
  const window = () => windows[0];
  const browser = {
    runtime: {onMessage: listener, sendNativeMessage: (_app, message) => nativeMessage(message)},
    extension: {inIncognitoContext: false, isAllowedIncognitoAccess: checkPrivate || (async () => privateAccess)},
    permissions: {contains: async () => grant},
    storage: {local: {get: async () => ({enabledProfile: profile}), remove: async () => {}}},
    tabs: {onRemoved: listener, onActivated: listener, get: async id => window()?.tabs.find(tab => tab.id === id),
      create: async ({url: target}) => {
        creates++;
        const tab = {...existing, id: 2, active: false, url: target};
        // Safari returns value snapshots; a later create cannot mutate the prior one.
        windows = [{...window(), tabs: [...window().tabs, tab]}];
        return tab;
      },
      remove: async () => { closes++; }},
    windows: {getAll: getAll || (async () => windows), get: async () => window(), onFocusChanged: listener},
    webNavigation: {onBeforeNavigate: listener, onCommitted: listener,
      getFrame: async () => ({url, documentId, parentFrameId: -1, errorOccurred: false})},
    scripting: {executeScript: async ({target, func}) => {
      scripts++;
      if (target.documentIds[0] !== documentId && !ignoreDocumentTarget) return [];
      return [{documentId, frameId: 0, result: func.name === "readPage" ? {status: "ok", final_url: url, text: "fixture"} : true}];
    }},
  };
  const context = vm.createContext({browser, crypto: {randomUUID}, URL, setTimeout() { return 1; }, clearTimeout() {}});
  vm.runInContext(readFileSync(join(__dirname, "../extension/shared/read-page.js"), "utf8"), context);
  vm.runInContext(readFileSync(join(__dirname, "Web/background.js"), "utf8"), context);
  await vm.runInContext("ready", context);
  const execute = vm.runInContext("execute", context);
  return {call: (operation, payload) => execute({operation, payload, expires: Date.now() + 10000}),
    poll: () => vm.runInContext("poll(enabledProfile)", context),
    disable: () => vm.runInContext('setup("disable")', context),
    profile, instance: vm.runInContext("instance", context),
    counters: () => ({scripts, creates, closes})};
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
  const request = {v: 1, id: "a".repeat(32), operation: "find", payload: {url}, expires: Date.now() + 10000};
  for (const [profile, instance] of [[randomUUID(), browser.instance], [browser.profile, randomUUID()]]) {
    supplied = {status: "claimed", profile, instance, request};
    await browser.poll();
  }
  assert.equal(messages.filter(message => message.phase === "result").length, 0);
  assert.equal(browser.counters().scripts, 0);
  supplied = {status: "claimed", profile: browser.profile, instance: browser.instance, request};
  await browser.poll();
  const results = messages.filter(message => message.phase === "result");
  assert.equal(results.length, 1);
  assert.equal(results[0].answer.status, "ok");
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
      request: {v: 1, id: "b".repeat(32), operation: "find", payload: {url}, expires: Date.now() + 10000}};
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
