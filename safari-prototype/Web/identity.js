"use strict";

const identity = document.getElementById("identity");
browser.runtime.sendMessage("probe-identity").then(value => {
    identity.textContent = [value.status, value.profile, value.instance].filter(Boolean).join("\n");
}, () => {
    identity.textContent = "Identity unavailable";
});
