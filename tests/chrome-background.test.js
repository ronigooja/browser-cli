const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");
const vm = require("node:vm");

const script = fs.readFileSync(path.join(__dirname, "../chrome-background/extension/background.js"), "utf8");
const noop = { addListener() {} };

function extension(tabs, windows, focused) {
  let receive, reply;
  const creations = [];
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
    windows: { getAll: async () => windows, getLastFocused: async () => focused },
  };
  vm.runInNewContext(script, { chrome, URL });
  return {
    creations,
    async open(url, expires = Date.now() + 1000) {
      await receive({ url, expires });
      return reply;
    },
  };
}

test("reuse a matching tab without changing it", async () => {
  const browser = extension([{ id: 5, url: "https://example.com/path" }], []);
  assert.deepEqual(await browser.open("https://example.com"), { id: 5, action: "reused" });
  assert.deepEqual(browser.creations, []);
});

test("create an inactive tab in the last focused normal window", async () => {
  const browser = extension([], [{ id: 1 }, { id: 2 }], { id: 2 });
  assert.deepEqual(await browser.open("https://example.com"), { id: 42, action: "created" });
  assert.deepEqual(browser.creations, [{ windowId: 2, url: "https://example.com/", active: false }]);
});

test("reject unsupported requests, stale requests and no-window creation", async () => {
  const empty = extension([], []);
  assert.match((await empty.open("file:///tmp/test")).error, /HTTP\(S\)/);
  assert.match((await empty.open("https://name:password@example.com")).error, /credentials/);
  assert.match((await empty.open("https://example.com")).error, /normal Chrome window/);
  const browser = extension([], [{ id: 1 }]);
  assert.match((await browser.open("https://example.com", 0)).error, /expired/);
  assert.deepEqual(browser.creations, []);
});
