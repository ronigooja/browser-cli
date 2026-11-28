"use strict";

const identity = document.getElementById("identity");
browser.runtime.sendMessage("probe-identity").then(value => {
    identity.textContent = [value.status, value.profile, value.instance].filter(Boolean).join("\n");
}, () => {
    identity.textContent = "Identity unavailable";
});

const mode = document.getElementById("mode");
const arm = document.getElementById("arm");
const warm = document.getElementById("warm");
const qualify = document.getElementById("qualify");
const push = document.getElementById("push");
async function control(message) {
    arm.disabled = warm.disabled = qualify.disabled = push.disabled = true;
    try {
        const value = await browser.runtime.sendMessage(message);
        if (value.status === "mode") {
            mode.textContent = `Mode: ${value.mode}; alarms: ${value.hasAlarms ? "available" : "unavailable"}; ` +
                `native port: ${value.pushConnected ? "open" : "closed"}`;
        }
        else if (value.status === "pull_armed") mode.textContent = `Pull armed for ${new Date(value.scheduledTime).toLocaleTimeString()}`;
        else if (value.status === "warm_pull_armed") mode.textContent = `Warm test armed; fallback at ${new Date(value.scheduledTime).toLocaleTimeString()}`;
        else if (value.status === "window_qualification_armed") {
            mode.textContent = `Qualification armed for 120s; no alarm. Profile ${value.profile}; instance ${value.instance}`;
        }
        else mode.textContent = value.status;
    } catch {
        mode.textContent = "Transport control unavailable";
    } finally {
        arm.disabled = warm.disabled = qualify.disabled = push.disabled = false;
    }
}
arm.addEventListener("click", () => control("probe-arm-pull"));
warm.addEventListener("click", () => control("probe-arm-warm"));
qualify.addEventListener("click", () => control("probe-arm-window-qualification"));
push.addEventListener("click", () => control("probe-use-push"));
control("probe-mode");
