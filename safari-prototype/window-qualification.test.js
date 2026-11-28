"use strict";

const assert = require("node:assert/strict");
const {readFileSync} = require("node:fs");
const test = require("node:test");
const vm = require("node:vm");

const script = readFileSync(require.resolve("./Web/background.js"), "utf8");
const instance = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa";
const profile = "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb";
const other = "cccccccc-cccc-cccc-cccc-cccccccccccc";
const fixture = {id: 17, type: "normal", incognito: false, focused: false};
const event = () => {
    const listeners = new Set();
    return {addListener: fn => listeners.add(fn), removeListener: fn => listeners.delete(fn),
        fire: value => listeners.forEach(fn => fn(value))};
};

async function run({arm = "probe-arm-window-qualification", grant = {}, before = [], after = [fixture],
    permission = true, privateAccess = false, onCreate = () => fixture, cancelBeforeCreate = false} = {}) {
    const created = event();
    const focus = event();
    const timers = new Map();
    const checks = [];
    const results = [];
    let tick = 0;
    let listener;
    const browser = {
        runtime: {id: "probe", getURL: () => "popup", onMessage: {addListener: fn => { listener = fn; }},
            connectNative() { throw new Error("Unexpected dispatch port"); },
            async sendNativeMessage(_app, message) {
                if (message.phase === "identify") return {status: "identified", profile, instance};
                if (message.phase === "pull") {
                    checks.push(message.wake);
                    return {status: "claimed", operation: "qualify-window", request: other, profile, instance,
                        expires: Date.now() / 1000 + 60, ...grant};
                }
                results.push(message.result);
            }},
        storage: {local: {get: async () => ({pullMode: true}), set: async () => {}, remove: async () => {}}},
        alarms: {onAlarm: event(), create: async () => {}, clear: async () => {}, get: async () => ({scheduledTime: 1})},
        extension: {inIncognitoContext: false, isAllowedIncognitoAccess: async () => privateAccess},
        permissions: {contains: async () => permission},
        windows: {onCreated: created, onFocusChanged: focus, async getAll(options) {
            checks.push(`inventory:${options.populate}`);
            if (cancelBeforeCreate) await control("probe-arm-pull");
            return checks.includes("create") ? after : before;
        }, async create(options) {
            assert.equal(JSON.stringify(options), JSON.stringify({url: "https://example.com/#safari-probe-created",
                type: "normal", focused: false, incognito: false}));
            checks.push("create");
            return onCreate({created, focus});
        }},
    };
    const control = message => new Promise(resolve => listener(message, {id: "probe", url: "popup"}, resolve));
    vm.runInNewContext(script, {browser, crypto: {randomUUID: () => instance}, performance: {now: () => 0},
        setTimeout(fn) { timers.set(++tick, fn); return tick; }, clearTimeout(id) { timers.delete(id); }});
    await control(arm);
    for (const [id, fn] of [...timers]) { timers.delete(id); await fn(); }
    return {checks, result: results[0], pendingTicks: timers.size};
}

test("separate warm arm attempts once and reports only a candidate", async () => {
    const {checks, result, pendingTicks} = await run({onCreate({created}) { created.fire(fixture); return fixture; }});
    assert.deepEqual(checks, ["qualification_poll", "inventory:false", "create", "inventory:false"]);
    assert.equal(result.status, "candidate_observed");
    assert.equal(result.beforeCount, 0);
    assert.equal(result.afterCount, 1);
    assert.equal(pendingTicks, 0);
});

test("requires a special arm, matching binding, privacy, site access and zero visible entries", async () => {
    for (const [args, status] of [
        [{arm: "probe-arm-warm"}, undefined], [{grant: {instance: other}}, undefined],
        [{grant: {profile: other}}, "qualification_not_armed"],
        [{privateAccess: true}, "private_access_enabled_or_unverified"],
        [{permission: false}, "fixture_access_not_granted"],
        [{before: [{}]}, "window_seen_before_operation"],
        [{cancelBeforeCreate: true}, "qualification_not_armed"],
    ]) {
        const {checks, result} = await run(args);
        assert.ok(!checks.includes("create"));
        assert.equal(result?.status, status);
    }
});

test("focus, incomplete evidence and API errors are unconfirmed and do not rearm", async () => {
    for (const args of [
        {onCreate({focus}) { focus.fire(fixture.id); return fixture; }},
        {onCreate({created}) { for (let i = 0; i < 17; ++i) created.fire(fixture); return fixture; }},
        {onCreate() { return {...fixture, focused: true}; }},
        {onCreate() { throw new Error("could have committed"); }},
        {after: [{...fixture, incognito: undefined}]},
        {after: []},
    ]) {
        const {checks, result, pendingTicks} = await run(args);
        assert.equal(checks.filter(check => check === "create").length, 1);
        assert.equal(result.status, "operation_outcome_unknown");
        assert.equal(pendingTicks, 0);
    }
});
