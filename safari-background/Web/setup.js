"use strict";

const origins = ["http://*/*", "https://*/*"];
const state = document.getElementById("state");
const enable = document.getElementById("enable");
const disable = document.getElementById("disable");
let eligible = false;

function show(result) {
  state.textContent = [result.status, result.profile].filter(Boolean).join("\n");
}

async function identify() {
  try {
    const result = await browser.runtime.sendMessage("identify");
    show(result);
    eligible = !!result.profile && browser.extension?.inIncognitoContext === false &&
      await browser.extension?.isAllowedIncognitoAccess?.() === false;
    enable.disabled = !eligible;
    if (result.profile && !eligible) state.textContent += "\nDisable private extension access first.";
  } catch (_) { show({status: "setup_unavailable"}); }
}

enable.addEventListener("click", async () => {
  if (!eligible) return;
  enable.disabled = disable.disabled = true;
  try {
    // Called directly in the user gesture. Never request permissions from an MCP operation.
    if (await browser.permissions.request({origins}) !== true) show({status: "site_access_required"});
    else show(await browser.runtime.sendMessage("enable"));
  } catch (_) { show({status: "site_access_unavailable"}); }
  finally { enable.disabled = !eligible; disable.disabled = false; }
});

disable.addEventListener("click", async () => {
  enable.disabled = disable.disabled = true;
  try { show(await browser.runtime.sendMessage("disable")); }
  catch (_) { show({status: "setup_unavailable"}); }
  finally { enable.disabled = !eligible; disable.disabled = false; }
});

identify();
