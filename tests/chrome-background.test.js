const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const { webcrypto } = require("node:crypto");
const test = require("node:test");
const vm = require("node:vm");

const script = fs.readFileSync(path.join(__dirname, "../chrome-background/extension/background.js"), "utf8");
const shared = fs.readFileSync(path.join(__dirname, "../extension/shared/read-page.js"), "utf8");
const noop = { addListener() {} };
const protocol = 3;

function production(initialTabs = [{ id: 1, url: "https://other.test/" }]) {
  const url = "https://example.com/";
  const tabs = new Map(initialTabs.map((tab, i) => [tab.id, {
    url, incognito: false, windowId: 1, active: i === 0, highlighted: i === 0, ...tab,
  }]));
  const events = {};
  const listener = name => ({ addListener(fn) { events[name] = fn; } });
  const calls = { create: [], remove: [], execute: [], snapshot: [] };
  let receive, disconnect, reply;
  let frame = { frameId: 0, documentId: "document-one", documentLifecycle: "active", errorOccurred: false, url };
  let probe = { status: "ok", final_url: url, text: "Visible text", title: "Example" };
  let duringInjection = () => {};
  let failClose = false;
  let allSites = true;
  let windows = [{ id: 1, type: "normal", incognito: false, focused: true }];
  const hooks = {};
  const populate = window => ({ ...window, tabs: [...tabs.values()].filter(tab => tab.windowId === window.id).map(tab => ({ ...tab })) });
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
    alarms: { onAlarm: listener("alarm"), create: () => {} },
    permissions: { contains: async ({ origins }) => {
      assert.equal(origins.join(","), "http://*/*,https://*/*");
      return allSites;
    } },
    tabs: {
      query: async options => { assert.equal(options.windowType, "normal"); return [...tabs.values()].map(tab => ({ ...tab })); },
      get: async id => {
        if (!tabs.has(id)) throw Error("gone");
        const tab = { ...tabs.get(id) };
        hooks.getTab?.();
        return tab;
      },
      create: async options => {
        calls.create.push({ ...options });
        if (options.active) for (const tab of tabs.values()) if (tab.windowId === options.windowId) tab.active = tab.highlighted = false;
        const tab = { id: 42, windowId: options.windowId, incognito: false, url: options.url,
          active: options.active, highlighted: options.active };
        tabs.set(tab.id, tab);
        const created = { ...tab };
        hooks.createTab?.();
        return created;
      },
      update: async (id, options) => {
        const tab = tabs.get(id);
        if (!tab) throw Error("gone");
        if (options.active) for (const item of tabs.values()) if (item.windowId === tab.windowId) item.active = item.highlighted = false;
        Object.assign(tab, options);
        if (options.active) tab.highlighted = true;
        return {...tab};
      },
      remove: async id => {
        calls.remove.push(id);
        if (failClose) throw Error("uncertain");
        hooks.removeTab?.();
        tabs.delete(id);
        events.remove(id);
      },
      onRemoved: listener("remove"), onReplaced: listener("replace"), onActivated: listener("activate"),
      onHighlighted: listener("highlight"), onAttached: listener("attach"), onDetached: listener("detach"),
      onMoved: listener("move"), onUpdated: listener("update"),
    },
    windows: {
      get: async id => {
        hooks.getWindow?.();
        return windows.find(window => window.id === id);
      },
      getAll: async options => {
        assert.equal(options.populate, true);
        calls.snapshot.push(options);
        const result = windows.map(populate);
        hooks.snapshot?.(calls.snapshot.length);
        return result;
      },
      getLastFocused: async () => windows[0],
      update: async (id, options) => {
        const window = windows.find(item => item.id === id);
        if (!window) throw Error("gone");
        if (options.focused) for (const item of windows) item.focused = false;
        Object.assign(window, options);
        return {...window};
      },
      create: async options => {
        calls.create.push({ ...options });
        const window = { id: 2, type: options.type, incognito: options.incognito, focused: options.focused };
        windows.push(window);
        tabs.set(43, { id: 43, windowId: window.id, incognito: false, url: options.url, active: true, highlighted: true });
        events.activate({ tabId: 43, windowId: window.id });
        events.highlight({ tabIds: [43], windowId: window.id });
        const created = populate(window);
        hooks.createWindow?.(window);
        return created;
      },
      onFocusChanged: listener("focus"),
    },
    webNavigation: {
      getFrame: async options => { assert.equal(options.frameId, 0); return frame; },
      onHistoryStateUpdated: { addListener(fn) { events.history = fn; } },
    },
    scripting: {
      executeScript: async ({ func, ...options }) => {
        assert.ok(func === context.readPage || func === context.agentBrowserPageOperation);
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
    vm.runInContext("function agentBrowserPageOperation() {}", context);
  } });
  vm.runInContext(script, context);
  return {
    url, tabs, calls, events, hooks,
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
      await receive({ v: protocol, id: "request", operation, payload, expires });
      assert.equal(reply.id, "request");
      assert.equal(reply.v, protocol);
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

test("list is bounded and validates regular tab metadata", async () => {
  const browser = production(Array.from({length: 35}, (_, id) => ({id: id + 1, url: `https://example.com/${id}`, title: `Tab ${id}`})));
  const result = await browser.request("list");
  assert.equal(result.status, "ok");
  assert.equal(result.tabs.length, 32);
  assert.equal(result.truncated, true);
  assert.equal(result.tabs[0].window_id, 1);
  assert.equal(result.tabs[0].active, true);
  assert.equal((await browser.request("list", {extra: true})).status, "invalid_request");
});

test("page pins its document and does not confirm a changed action", async () => {
  const browser = production([{id: 1, url: "https://example.com/", title: "Example"}]);
  const list = await browser.request("list");
  const payload = {ref: list.tabs[0].ref, expected_url: browser.url, kind: "snapshot"};
  assert.equal((await browser.request("page", payload)).status, "ok");
  assert.deepEqual(browser.calls.execute[0].target.documentIds, ["document-one"]);
  browser.duringInjection(() => browser.frame({documentId: "document-two"}));
  assert.equal((await browser.request("page", payload)).status, "read_unavailable");
  browser.duringInjection(() => browser.frame({documentId: "document-three"}));
  assert.equal((await browser.request("page", {...payload, kind: "click", selector: "button"})).status, "action_unconfirmed");
  assert.equal((await browser.request("page", {...payload, script: "alert(1)"})).status, "invalid_request");
});

test("explicit foreground open creates an active tab without cleanup ownership", async () => {
  const browser = production();
  const result = await browser.request("front_open", {url: browser.url});
  assert.equal(result.status, "ok");
  assert.equal(result.created, true);
  assert.equal(result.tab.url, browser.url);
  assert.equal(result.ownership, undefined);
  assert.equal(browser.calls.create[0].active, true);
  assert.equal((await browser.request("front_open", {url: browser.url, extra: true})).status, "invalid_request");
});

test("foreground open never confirms a redirected or uncertain created tab", async () => {
  const browser = production();
  browser.hooks.createTab = () => { browser.tabs.get(42).url = "https://elsewhere.test/"; };
  assert.equal((await browser.request("front_open", {url: browser.url})).status, "open_unconfirmed");
  assert.equal(browser.calls.create.length, 1);
});

test("foreground open reuses one exact existing tab and refuses ambiguity", async () => {
  const browser = production([{id: 1, url: "https://example.com/", title: "Example"}]);
  const result = await browser.request("front_open", {url: browser.url});
  assert.equal(result.status, "ok");
  assert.equal(result.created, false);
  assert.equal(browser.calls.create.length, 0);
  const ambiguous = production([{id: 1, url: browser.url}, {id: 2, url: browser.url}]);
  assert.equal((await ambiguous.request("front_open", {url: browser.url})).status, "tab_ambiguous");
  assert.equal(ambiguous.calls.create.length, 0);
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

test("created tabs preserve selection and focus and only one cleanup attempt is allowed", async () => {
  const browser = production();
  const opened = await browser.request("open", { url: browser.url });
  assert.equal(opened.created, true);
  assert.equal(browser.tabs.get(1).active, true);
  assert.equal(browser.tabs.get(42).active, false);
  assert.equal(browser.calls.snapshot.length, 2);
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

test("cleanup ownership is irrevocably relinquished on tab selection or adoption", async () => {
  for (const adopt of [
    b => b.events.activate({ tabId: 42, windowId: 1 }),
    b => b.events.highlight({ tabIds: [1, 42], windowId: 1 }),
    b => b.events.detach(42, { oldWindowId: 1 }),
    b => b.events.attach(42, { newWindowId: 1 }),
    b => b.events.move(42, { windowId: 1 }),
    b => b.events.update(42, { pinned: true }),
    b => b.events.update(42, { groupId: 3 }),
  ]) {
    const browser = production();
    const opened = await browser.request("open", { url: browser.url });
    assert.equal(opened.status, "ok");
    adopt(browser); // The tab looks inactive and in the original window again by cleanup time.
    const payload = { ref: opened.tab.ref, ownership: opened.ownership };
    assert.equal((await browser.request("close", payload)).status, "cleanup_unconfirmed");
    assert.deepEqual(browser.calls.remove, []);
  }
});

test("cleanup refuses insufficient or changed final tab state even without an event", async () => {
  for (const override of [{ active: true }, { active: undefined }, { highlighted: true }, { windowId: 2 }, { incognito: true }]) {
    const browser = production();
    const opened = await browser.request("open", { url: browser.url });
    Object.assign(browser.tabs.get(42), override);
    const payload = { ref: opened.tab.ref, ownership: opened.ownership };
    assert.equal((await browser.request("close", payload)).status, "cleanup_unconfirmed");
    Object.assign(browser.tabs.get(42), { active: false, highlighted: false, windowId: 1, incognito: false });
    assert.equal((await browser.request("close", payload)).status, "cleanup_unconfirmed");
    assert.deepEqual(browser.calls.remove, []);
  }
});

test("cleanup detects adoption and identity races during asynchronous checks", async () => {
  for (const [hook, change, status, removals] of [
    ["getWindow", b => b.events.activate({ tabId: 42, windowId: 1 }), "cleanup_unconfirmed", []],
    ["getTab", b => b.events.detach(42, { oldWindowId: 1 }), "cleanup_unconfirmed", []],
    ["getTab", b => b.events.replace(42, 42), "cleanup_tab_not_found", []],
    ["removeTab", b => b.events.activate({ tabId: 42, windowId: 1 }), "cleanup_unconfirmed", [42]],
    ["removeTab", b => b.events.replace(99, 42), "cleanup_unconfirmed", [42]],
  ]) {
    const browser = production();
    const opened = await browser.request("open", { url: browser.url });
    browser.hooks[hook] = () => change(browser);
    assert.equal((await browser.request("close", { ref: opened.tab.ref, ownership: opened.ownership })).status, status);
    assert.deepEqual(browser.calls.remove, removals);
  }
});

test("background open returns uncertainty without ownership or fallback on changed evidence", async () => {
  for (const race of [
    b => { b.tabs.get(42).active = true; b.tabs.get(1).active = false; },
    b => { b.tabs.get(42).highlighted = true; },
    b => { b.tabs.get(1).highlighted = false; },
    b => { b.tabs.get(42).windowId = 2; },
    b => b.events.activate({ tabId: 42, windowId: 1 }),
    b => b.events.replace(99, 42),
    b => b.events.focus(-1),
    b => { b.hooks.snapshot = count => { if (count === 2) b.events.activate({ tabId: 42, windowId: 1 }); }; },
    b => b.windows([{ id: 1, type: "normal", incognito: false, focused: false }]),
    b => { b.hooks.snapshot = count => { if (count === 2) throw Error("lost snapshot"); }; },
    b => { throw Error("unknown create outcome"); },
  ]) {
    const browser = production();
    browser.hooks.createTab = () => race(browser);
    assert.deepEqual(await browser.request("open", { url: browser.url }), { status: "open_unconfirmed" });
    assert.deepEqual(browser.calls.create, [{ windowId: 1, url: browser.url, active: false }]);
    assert.deepEqual(browser.calls.remove, []);
  }
});

test("background open does not write when the pre-open selection or focus cannot be confirmed", async () => {
  for (const invalidate of [
    b => { b.tabs.get(1).active = false; },
    b => b.windows([{ id: 1, type: "normal", incognito: false }]),
    b => { b.hooks.snapshot = () => b.events.focus(1); },
  ]) {
    const browser = production();
    invalidate(browser);
    assert.deepEqual(await browser.request("open", { url: browser.url }), { status: "window_snapshot_unavailable" });
    assert.deepEqual(browser.calls.create, []);
  }
});

test("zero-window open verifies the sole new window remains unfocused and grants no active-tab cleanup", async () => {
  const browser = production([]);
  browser.windows([]);
  const opened = await browser.request("open", { url: browser.url });
  assert.equal(opened.status, "ok");
  assert.equal(opened.created, true);
  assert.equal(opened.tab.url, browser.url);
  assert.equal("ownership" in opened, false);
  assert.equal(browser.tabs.get(43).active, true);
  assert.equal(browser.calls.snapshot.length, 2);
  assert.deepEqual(browser.calls.create, [{ url: browser.url, type: "normal", incognito: false, focused: false }]);
  assert.equal((await browser.request("close", { ref: opened.tab.ref, ownership: webcrypto.randomUUID() })).status, "cleanup_unconfirmed");
  assert.deepEqual(browser.calls.remove, []);
});

test("zero-window focus changes or inconclusive observations never trigger a fallback", async () => {
  for (const race of [
    (b, window) => { window.focused = true; },
    (b, window) => { b.events.focus(window.id); b.events.focus(-1); },
    b => { b.windows([]); },
    b => { b.tabs.set(44, { ...b.tabs.get(43), id: 44, active: false }); },
    () => { throw Error("unknown create outcome"); },
  ]) {
    const browser = production([]);
    browser.windows([]);
    browser.hooks.createWindow = window => race(browser, window);
    assert.deepEqual(await browser.request("open", { url: browser.url }), { status: "open_unconfirmed" });
    assert.deepEqual(browser.calls.create, [{ url: browser.url, type: "normal", incognito: false, focused: false }]);
    assert.deepEqual(browser.calls.remove, []);
  }
});

test("an ineligible visible window is not treated as zero windows", async () => {
  const browser = production([]);
  browser.windows([{ id: 1, type: "popup", incognito: false, focused: true }]);
  assert.deepEqual(await browser.request("open", { url: browser.url }), { status: "no_regular_window" });
  assert.deepEqual(browser.calls.create, []);
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
    assert.ok(JSON.stringify({ v: protocol, id: "request", ...read }).replace(/[<>&\x7f-\uffff]/g, "\\u0000").length <= 65536);
  }
});

test("unversioned and prior-version writes are rejected without browser activity", async () => {
  const browser = production();
  assert.deepEqual(await browser.raw({ url: browser.url, expires: Date.now() + 1000 }),
    { v: protocol, id: "", status: "protocol_mismatch" });
  for (const operation of ["open", "close"]) {
    assert.deepEqual(await browser.raw({ v: 1, id: "old", operation, payload: { url: browser.url }, expires: Date.now() + 1000 }),
      { v: protocol, id: "old", status: "protocol_mismatch" });
  }
  assert.deepEqual(browser.calls.create, []);
  assert.deepEqual(browser.calls.remove, []);
});
