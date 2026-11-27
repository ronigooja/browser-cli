const host = "com.browser_cli.background";
const reconnect = "reconnect-native-host";
let port;

async function openBackground({ url, expires }) {
  if (typeof url !== "string" || !/^https?:\/\//i.test(url)) {
    throw new Error("Chrome background open requires an HTTP(S) URL");
  }
  const target = new URL(url);
  if (target.username || target.password) {
    throw new Error("URLs with embedded credentials are not supported");
  }

  const tabs = await chrome.tabs.query({ windowType: "normal" });
  const match = tabs.find(tab => !tab.incognito &&
    (tab.pendingUrl ?? tab.url)?.startsWith(target.href));
  if (match) return { id: match.id, action: "reused" };

  const normal = { windowTypes: ["normal"] };
  const windows = (await chrome.windows.getAll(normal)).filter(window => !window.incognito);
  if (!windows.length) {
    throw new Error("Open a normal Chrome window in the profile with the extension first");
  }
  const lastFocused = await chrome.windows.getLastFocused(normal).catch(() => null);
  const window = windows.find(window => window.id === lastFocused?.id) ?? windows[0];
  if (!Number.isFinite(expires) || Date.now() >= expires) {
    throw new Error("Chrome background request expired before opening a tab");
  }
  const tab = await chrome.tabs.create({ windowId: window.id, url: target.href, active: false });
  return { id: tab.id, action: "created" };
}

function connect() {
  if (port) return;
  const connection = chrome.runtime.connectNative(host);
  port = connection;
  connection.onMessage.addListener(async request => {
    let response;
    try {
      response = await openBackground(request);
    } catch (error) {
      response = { error: error.message };
    }
    if (port === connection) connection.postMessage(response);
  });
  connection.onDisconnect.addListener(() => {
    // Reading lastError suppresses the warning when the host isn't installed yet.
    void chrome.runtime.lastError;
    port = undefined;
    chrome.alarms.create(reconnect, { delayInMinutes: 0.5 });
  });
}

chrome.alarms.onAlarm.addListener(alarm => {
  if (alarm.name === reconnect) connect();
});
chrome.runtime.onStartup.addListener(connect);
chrome.runtime.onInstalled.addListener(connect);
connect();
