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

async function harness({windows = [], grant = true, privateAccess = false, ignoreDocumentTarget = false} = {}) {
  let scripts = 0, creates = 0, closes = 0;
  const window = () => windows[0];
  const browser = {
    runtime: {onMessage: listener},
    extension: {inIncognitoContext: false, isAllowedIncognitoAccess: async () => privateAccess},
    permissions: {contains: async () => grant},
    storage: {local: {get: async () => ({enabledProfile: randomUUID()})}},
    tabs: {onRemoved: listener, onActivated: listener, get: async id => window()?.tabs.find(tab => tab.id === id),
      create: async ({url: target}) => {
        creates++;
        const tab = {...existing, id: 2, active: false, url: target};
        // Safari returns value snapshots; a later create cannot mutate the prior one.
        windows = [{...window(), tabs: [...window().tabs, tab]}];
        return tab;
      },
      remove: async () => { closes++; }},
    windows: {getAll: async () => windows, get: async () => window(), onFocusChanged: listener},
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
