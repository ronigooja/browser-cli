const grant = { origins: ["<all_urls>"] };
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

function render(on) {
  toggle.checked = on;
  stateText.textContent = on ? "On" : "Off";
  detail.textContent = on
    ? "Screenshots of all sites are allowed until you turn this off."
    : "Screenshots are blocked. Reads of regular tabs work without this setting.";
}

// Reflects the actual optional grant, never the last requested switch position.
async function refresh() {
  try {
    render(await chrome.permissions.contains(grant));
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
  // Chrome requires this call to stay inside the click gesture, so nothing may await before it.
  let pending;
  try {
    pending = wanted ? chrome.permissions.request(grant) : chrome.permissions.remove(grant);
  } catch (error) {
    pending = Promise.reject(error);
  }
  toggle.disabled = true;
  pending
    .then(changed => {
      if (wanted && changed !== true) showError("Screenshot access was not granted. The switch is off.");
    }, () => {
      showError(wanted ? "Chrome could not turn on screenshot access." : "Chrome could not turn off screenshot access.");
    })
    .then(refresh);
});

chrome.permissions.onAdded?.addListener(refresh);
chrome.permissions.onRemoved?.addListener(refresh);

refresh();
