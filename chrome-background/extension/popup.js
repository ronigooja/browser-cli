const grant = { origins: ["<all_urls>"] };
const baseline = { origins: ["http://*/*", "https://*/*"] };
const toggle = document.getElementById("capture-toggle");
const stateText = document.getElementById("state");
const detail = document.getElementById("detail");
const errorText = document.getElementById("error");

function showError(message) {
  errorText.textContent = message;
  errorText.hidden = false;
}

function clearError() {
  errorText.textContent = "";
  errorText.hidden = true;
}

async function refresh() {
  try {
    const [settings, allowed, siteAccess] = await Promise.all([
      chrome.storage.local.get("captureEnabled"), chrome.permissions.contains(grant), chrome.permissions.contains(baseline),
    ]);
    const on = settings.captureEnabled === true && allowed && siteAccess;
    toggle.checked = on;
    stateText.textContent = on ? "On" : "Off";
    detail.textContent = !siteAccess
      ? "Chrome site access is unavailable. Check this extension's site access in Chrome settings; reads and screenshots need regular site access."
      : settings.captureEnabled === true && !allowed
        ? "Screenshot permission is missing. Turn the switch on to request it."
        : on ? "Screenshots are allowed until you turn this off."
          : "Screenshots are blocked. Reads of regular tabs work without this setting.";
    toggle.disabled = false;
  } catch (_) {
    stateText.textContent = "Unknown";
    detail.textContent = "Chrome could not report screenshot access. Close and reopen this popup to try again.";
    toggle.disabled = true;
  }
}

toggle.addEventListener("change", () => {
  clearError();
  const wanted = toggle.checked;
  // Request must be invoked synchronously in the user gesture.
  let pending;
  try {
    pending = wanted ? chrome.permissions.request(grant) : chrome.storage.local.set({ captureEnabled: false });
  } catch (error) {
    pending = Promise.reject(error);
  }
  toggle.disabled = true;
  Promise.resolve(pending).then(async granted => {
    if (wanted) {
      if (granted !== true) {
        await chrome.storage.local.set({ captureEnabled: false });
        showError("Screenshot access was not granted. The switch is off.");
      } else {
        await chrome.storage.local.set({ captureEnabled: true });
      }
    }
  }).catch(() => {
    showError(wanted ? "Chrome could not turn on screenshot access." : "Chrome could not turn off screenshot access.");
  }).then(refresh);
});

chrome.permissions.onAdded?.addListener(refresh);
chrome.permissions.onRemoved?.addListener(refresh);
chrome.storage.onChanged.addListener(refresh);
refresh();
