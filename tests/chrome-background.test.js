const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const { webcrypto } = require("node:crypto");
const test = require("node:test");
const vm = require("node:vm");

const script = fs.readFileSync(path.join(__dirname, "../chrome-background/extension/background.js"), "utf8");
const shared = fs.readFileSync(path.join(__dirname, "../extension/shared/read-page.js"), "utf8");
const noop = { addListener() {} };

function extension(tabs, windows, focused, createdWindow = { tabs: [{ id: 43 }] }) {
  let receive, reply;
  const creations = [];
  const windowCreations = [];
  const port = {
    onMessage: { addListener(fn) { receive = fn; } },
    onDisconnect: noop,
    postMessage(message) { reply = JSON.parse(JSON.stringify(message)); },
  };
  const chrome = {
    runtime: { connectNative: () => port, onStartup: noop, onInstalled: noop },
    alarms: { onAlarm: noop },
    tabs: {
      query: async options => { assert.equal(options.windowType, "normal"); return tabs; },
      create: async options => { creations.push({ ...options }); return { id: 42 }; },
    },
    windows: {
      getAll: async () => windows,
      getLastFocused: async () => focused,
      create: async options => { windowCreations.push({ ...options }); return createdWindow; },
    },
  };
  vm.runInNewContext(script, { chrome, URL, crypto: webcrypto, importScripts() {} });
  return {
    creations,
    windowCreations,
    async open(url, expires = Date.now() + 1000) {
      await receive({ url, expires });
      return reply;
    },
  };
}

test("reuse a matching tab without changing it", async () => {
  const browser = extension([{ id: 4, url: "https://example.com/path" },
    { id: 5, url: "https://example.com/" }], []);
  assert.deepEqual(await browser.open("https://example.com"), { id: 5, action: "reused" });
  assert.deepEqual(browser.creations, []);
});

test("a Chrome profile path prefix does not reuse an individual post tab", async () => {
  const browser = extension([
    { id: 5, url: "https://example.com/user/status/123" },
    { id: 6, url: "https://example.com/user?view=posts" },
    { id: 7, url: "https://example.com/user#posts" },
  ], [{ id: 1 }]);
  assert.deepEqual(await browser.open("https://example.com/user"), { id: 42, action: "created" });
  assert.deepEqual(browser.creations, [{ windowId: 1, url: "https://example.com/user", active: false }]);
});

test("prefer the exact pending navigation over the committed URL", async () => {
  const browser = extension([
    { id: 4, url: "https://example.com/user", pendingUrl: "https://example.com/user/status/123" },
    { id: 5, url: "https://example.com/user/status/456", pendingUrl: "https://example.com/user" },
  ], []);
  assert.deepEqual(await browser.open("https://example.com/user"), { id: 5, action: "reused" });
  assert.deepEqual(browser.creations, []);
});

test("create an inactive tab in the last focused normal window", async () => {
  const browser = extension([], [{ id: 1 }, { id: 2 }], { id: 2 });
  assert.deepEqual(await browser.open("https://example.com"), { id: 42, action: "created" });
  assert.deepEqual(browser.creations, [{ windowId: 2, url: "https://example.com/", active: false }]);
});

test("create an unfocused normal window when no regular window exists", async () => {
  const browser = extension([], [{ id: 1, incognito: true }]);
  assert.deepEqual(await browser.open("https://example.com"), { id: 43, action: "created" });
  assert.deepEqual(browser.windowCreations, [{ url: "https://example.com/", type: "normal", incognito: false, focused: false }]);
  assert.deepEqual(browser.creations, []);
});

test("reject malformed window creation responses", async () => {
  for (const result of [null, {}, { tabs: [] }, { tabs: [{ id: 1 }, { id: 2 }] }, { tabs: [{}] },
    { tabs: [{ id: 1.5 }] }, { tabs: [{ id: -1 }] }]) {
    const browser = extension([], [], undefined, result);
    assert.match((await browser.open("https://example.com")).error, /single created tab.*integer ID/);
    assert.equal(browser.windowCreations.length, 1);
  }
});

test("reject unsupported and stale requests", async () => {
  const empty = extension([], []);
  assert.match((await empty.open("file:///tmp/test")).error, /HTTP\(S\)/);
  assert.match((await empty.open("https://name:password@example.com")).error, /credentials/);
  assert.match((await empty.open("https://example.com", 0)).error, /expired/);
  assert.deepEqual(empty.windowCreations, []);
  const browser = extension([], [{ id: 1 }]);
  assert.match((await browser.open("https://example.com", 0)).error, /expired/);
  assert.deepEqual(browser.creations, []);
});

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
    windows(value) { windows = value; },
    reconnect() { disconnect(); events.alarm({ name: "reconnect-native-host" }); },
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
