const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const { webcrypto } = require("node:crypto");
const test = require("node:test");
const vm = require("node:vm");

const script = fs.readFileSync(path.join(__dirname, "../chrome-background/extension/background.js"), "utf8");
const shared = fs.readFileSync(path.join(__dirname, "../extension/shared/read-page.js"), "utf8");
const noop = { addListener() {} };

function production(initialTabs = []) {
  const url = "https://example.com/";
  const tabs = new Map(initialTabs.map(tab => [tab.id, { url, incognito: false, windowId: 1, ...tab }]));
  const events = {};
  const alarm = { addListener(fn) { events.alarm = fn; } };
  const calls = { create: [], remove: [], execute: [] };
  let receive, disconnect, reply;
  let frame = { frameId: 0, documentId: "document-one", documentLifecycle: "active", errorOccurred: false, url };
  let probe = { status: "ok", final_url: url, text: "Visible text", title: "Example" };
  let duringInjection = () => {};
  let failClose = false;
  let allSites = true;
  let windows = [{ id: 1, type: "normal", incognito: false }];
  let context;
  const chrome = {
    runtime: {
      connectNative() {
        return { onMessage: { addListener(fn) { receive = fn; } },
          onDisconnect: { addListener(fn) { disconnect = fn; } },
          postMessage(value) { reply = JSON.parse(JSON.stringify(value)); } };
      },
      onInstalled: noop, onStartup: noop,
    },
    alarms: { onAlarm: alarm, create: () => {} },
    permissions: { contains: async ({ origins }) => {
      assert.equal(origins.join(","), "http://*/*,https://*/*");
      return allSites;
    } },
    tabs: {
      query: async options => { assert.equal(options.windowType, "normal"); return [...tabs.values()]; },
      get: async id => { if (!tabs.has(id)) throw Error("gone"); return { ...tabs.get(id) }; },
      create: async options => {
        calls.create.push({ ...options });
        const tab = { id: 42, windowId: 1, incognito: false, url: options.url };
        tabs.set(tab.id, tab);
        return tab;
      },
      remove: async id => {
        calls.remove.push(id);
        if (failClose) throw Error("uncertain");
        tabs.delete(id);
        events.remove(id);
      },
      onRemoved: { addListener(fn) { events.remove = fn; } },
      onReplaced: { addListener(fn) { events.replace = fn; } },
    },
    windows: {
      get: async id => windows.find(window => window.id === id),
      getAll: async () => windows,
      getLastFocused: async () => windows[0],
      create: async options => {
        calls.create.push({ ...options });
        return { incognito: false, tabs: [{ id: 43, incognito: false, url: options.url }] };
      },
    },
    webNavigation: {
      getFrame: async options => { assert.equal(options.frameId, 0); return frame; },
      onHistoryStateUpdated: { addListener(fn) { events.history = fn; } },
    },
    scripting: {
      executeScript: async ({ func, ...options }) => {
        assert.equal(func, context.readPage);
        calls.execute.push(JSON.parse(JSON.stringify(options)));
        const documentId = frame.documentId;
        duringInjection();
        return [{ documentId, frameId: 0, result: probe }];
      },
    },
  };
  context = vm.createContext({ chrome, URL, crypto: webcrypto, importScripts(name) {
    assert.equal(name, "read-page.js");
    vm.runInContext(shared, context);
  } });
  vm.runInContext(script, context);
  return {
    url, tabs, calls, events,
    frame(next) { frame = { ...frame, ...next }; },
    probe(next) { probe = { ...probe, ...next }; },
    duringInjection(fn) { duringInjection = fn; },
    failClose() { failClose = true; },
    setAllSites(value) { allSites = value; },
    windows(value) { windows = value; },
    reconnect() { disconnect(); events.alarm({ name: "reconnect-native-host" }); },
    async raw(message) { await receive(message); return reply; },
    async request(operation, payload = {}, expires = Date.now() + 1000) {
      reply = undefined;
      await receive({ v: 1, id: "request", operation, payload, expires });
      assert.equal(reply.id, "request");
      assert.equal(reply.v, 1);
      const { status, ...data } = reply;
      delete data.v;
      delete data.id;
      return { status, ...data };
    },
  };
}

test("setup and page operations refuse missing all-site access without opening a tab", async () => {
  const browser = production();
  browser.setAllSites(false);
  assert.equal((await browser.request("status")).status, "permission_required");
  assert.equal((await browser.request("open", { url: browser.url })).status, "permission_required");
  assert.deepEqual(browser.calls.create, []);
  browser.setAllSites(true);
  assert.equal((await browser.request("status")).status, "ok");
});

test("find returns same origin and path candidates with generation-scoped references and excludes private tabs", async () => {
  const browser = production([{ id: 1 }, { id: 2, url: "https://example.com/?added=1" },
    { id: 3, incognito: true }, { id: 4, url: "https://example.com/child" },
    { id: 5, pendingUrl: "https://other.test/" }]);
  assert.deepEqual(await browser.request("status"), { status: "ok" });
  const found = await browser.request("find", { url: "https://EXAMPLE.COM:443" });
  assert.deepEqual(found.tabs.map(tab => tab.url), [browser.url, browser.url + "?added=1"]);
  assert.notEqual(found.tabs[0].ref, found.tabs[1].ref);
  const reused = await browser.request("open", { url: browser.url });
  assert.deepEqual(reused, { status: "ok", tab: found.tabs[0], created: false });
  assert.deepEqual(await browser.request("close", { ref: reused.tab.ref, ownership: webcrypto.randomUUID() }),
    { status: "cleanup_unconfirmed" });
  assert.deepEqual(browser.calls.remove, []);
  browser.reconnect();
  assert.notEqual((await browser.request("find", { url: browser.url })).tabs[0].ref, found.tabs[0].ref);
});

test("created tabs stay inactive and only one valid cleanup attempt is allowed", async () => {
  const browser = production();
  const opened = await browser.request("open", { url: browser.url });
  assert.equal(opened.created, true);
  assert.deepEqual(browser.calls.create, [{ windowId: 1, url: browser.url, active: false }]);
  const payload = { ref: opened.tab.ref, ownership: opened.ownership };
  browser.failClose();
  assert.equal((await browser.request("close", payload)).status, "cleanup_unconfirmed");
  assert.equal((await browser.request("close", payload)).status, "cleanup_unconfirmed");
  assert.deepEqual(browser.calls.remove, [42]);
  browser.reconnect();
  assert.equal((await browser.request("close", payload)).status, "cleanup_instance_changed");
  assert.deepEqual(browser.calls.remove, [42]);
});

test("close reports success once and never closes a replacement tab", async () => {
  const browser = production();
  const opened = await browser.request("open", { url: browser.url });
  const payload = { ref: opened.tab.ref, ownership: opened.ownership };
  assert.equal((await browser.request("close", payload)).status, "closed");
  assert.equal((await browser.request("close", payload)).status, "cleanup_tab_not_found");
  const newer = await browser.request("open", { url: browser.url });
  browser.events.replace(42, 42);
  assert.equal((await browser.request("close", { ref: newer.tab.ref, ownership: newer.ownership })).status, "cleanup_tab_not_found");
  assert.deepEqual(browser.calls.remove, [42]);
});

test("read pins the shared isolated function to the observed main document", async () => {
  const browser = production([{ id: 1 }]);
  const { tabs: [tab] } = await browser.request("find", { url: browser.url });
  const args = { ref: tab.ref, expected_url: browser.url, selector: "main", wait_for_selector: "#ready", include_links: true };
  assert.equal((await browser.request("read", args)).text, "Visible text");
  assert.deepEqual(browser.calls.execute, [{ target: { tabId: 1, documentIds: ["document-one"] }, world: "ISOLATED",
    args: [{ expectedURL: browser.url, selector: "main", waitForSelector: "#ready", includeLinks: true }] }]);
  browser.duringInjection(() => browser.frame({ documentId: "document-two" }));
  assert.deepEqual(await browser.request("read", args), { status: "target_changed", final_url: browser.url });
  browser.duringInjection(() => browser.events.history({ tabId: 1, frameId: 0 }));
  assert.deepEqual(await browser.request("read", args), { status: "target_changed", final_url: browser.url });
  browser.frame({ documentId: undefined });
  assert.equal((await browser.request("read", args)).status, "read_unavailable");
  assert.equal(browser.calls.execute.length, 3);
});

test("read does not inject into a private, changed, or pending target or accept arbitrary code", async () => {
  const browser = production([{ id: 1 }]);
  const { tabs: [tab] } = await browser.request("find", { url: browser.url });
  const args = { ref: tab.ref, expected_url: browser.url };
  assert.equal((await browser.request("read", { ...args, code: "document.cookie" })).status, "invalid_request");
  browser.tabs.get(1).pendingUrl = browser.url;
  assert.equal((await browser.request("read", args)).status, "page_loading");
  browser.tabs.get(1).pendingUrl = "https://other.test/";
  assert.deepEqual(await browser.request("read", args), { status: "target_changed", final_url: "https://other.test/" });
  browser.tabs.get(1).incognito = true;
  assert.deepEqual(await browser.request("read", args), { status: "target_changed" });
  assert.deepEqual(browser.calls.execute, []);
});

test("read remains framed with worst-case JSON escapes rather than dropping the connection", async () => {
  const browser = production([{ id: 1 }]);
  const { tabs: [tab] } = await browser.request("find", { url: browser.url });
  const args = { ref: tab.ref, expected_url: browser.url };
  for (const char of ["\u0000", "界", "<", "😄"]) {
    browser.probe({ text: char.repeat(12000), title: char.repeat(500), links: [{ url: browser.url, text: char.repeat(200) }] });
    const read = await browser.request("read", args);
    assert.equal(read.status, "ok");
    assert.equal(read.truncated, true);
    assert.ok(read.text.length > 0);
    assert.ok(JSON.stringify({ v: 1, id: "request", ...read }).replace(/[<>&\x7f-\uffff]/g, "\\u0000").length <= 65536);
  }
});

 test("unversioned open is rejected without browser activity", async () => {
  const browser = production();
  assert.deepEqual(await browser.raw({ url: browser.url, expires: Date.now() + 1000 }),
    { v: 1, id: "", status: "protocol_mismatch" });
  assert.deepEqual(browser.calls.create, []);
});
