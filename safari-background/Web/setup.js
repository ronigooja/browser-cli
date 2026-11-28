"use strict";

const state = document.getElementById("state");
const enable = document.getElementById("enable");
const disable = document.getElementById("disable");
const arm = document.getElementById("arm");
let eligible = false;
let enabled = false;

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
    enabled = result.status === "enabled";
    arm.disabled = !eligible || !enabled;
    if (result.profile && !eligible) state.textContent += "\nDisable private extension access first.";
  } catch (_) { show({status: "setup_unavailable"}); }
}

enable.addEventListener("click", async () => {
  if (!eligible) return;
  enable.disabled = disable.disabled = arm.disabled = true;
  try {
    const result = await browser.runtime.sendMessage("enable");
    enabled = result.status === "enabled";
    show(result);
  } catch (_) { show({status: "site_access_unavailable"}); }
  finally { enable.disabled = !eligible; disable.disabled = false; arm.disabled = !eligible || !enabled; }
});

disable.addEventListener("click", async () => {
  enable.disabled = disable.disabled = arm.disabled = true;
  enabled = false;
  try { show(await browser.runtime.sendMessage("disable")); }
  catch (_) { show({status: "setup_unavailable"}); }
  finally { enable.disabled = !eligible; disable.disabled = false; }
});

arm.addEventListener("click", async event => {
  if (!event.isTrusted || !eligible || !enabled) return;
  enable.disabled = disable.disabled = arm.disabled = true;
  try { show(await browser.runtime.sendMessage("arm_qualification")); }
  catch (_) { show({status: "setup_unavailable"}); }
  finally { enable.disabled = !eligible; disable.disabled = false; arm.disabled = !eligible || !enabled; }
});

identify();
