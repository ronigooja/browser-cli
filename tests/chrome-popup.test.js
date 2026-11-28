const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const test = require('node:test');
const vm = require('node:vm');
const dir = path.join(__dirname, '../chrome-background/extension');
const script = fs.readFileSync(path.join(dir, 'popup.js'), 'utf8');
const manifest = require(path.join(dir, 'manifest.json'));
const flush = () => new Promise(resolve => setImmediate(resolve));

function popup(initial = false) {
  let granted = initial;
  const calls = [];
  const elements = Object.fromEntries(['capture-toggle', 'state', 'detail', 'error'].map(id => [id, {
    checked: false, disabled: true, hidden: true, textContent: '',
    addEventListener(type, fn) { this[type] = fn; },
  }]));
  let enabled = false;
  const storage = { local: { get: async () => ({ captureEnabled: enabled }), set: async value => { enabled = value.captureEnabled; calls.push(["set", enabled]); } }, onChanged: { addListener() {} } };
  const permissions = {
    contains: async grant => { calls.push(['contains', JSON.stringify(grant)]); return grant.origins[0] === "<all_urls>" ? granted : true; },
    request: grant => { calls.push(['request', JSON.stringify(grant)]); granted = true; return Promise.resolve(true); },
    remove: grant => { calls.push(['remove', JSON.stringify(grant)]); granted = false; return Promise.resolve(true); },
    onAdded: { addListener() {} }, onRemoved: { addListener() {} },
  };
  vm.runInNewContext(script, { document: { getElementById: id => elements[id] }, chrome: { permissions, storage } });
  return { elements, calls, permissions };
}

test('popup toggles only optional all-URLs grant, retaining required host declarations', async () => {
  assert.deepEqual(manifest.host_permissions, ['http://*/*', 'https://*/*']);
  assert.deepEqual(manifest.optional_host_permissions, ['<all_urls>']);
  const { elements, calls } = popup();
  await flush();
  const toggle = elements['capture-toggle'];
  assert.equal(toggle.checked, false);
  toggle.checked = true;
  toggle.change();
  assert.deepEqual(calls.at(-1), ['request', '{"origins":["<all_urls>"]}']);
  await flush();
  assert.equal(toggle.checked, true);
  toggle.checked = false;
  toggle.change();
  assert.deepEqual(calls.at(-1), ['set', false]);
  assert.equal(calls.some(call => call[0] === 'remove'), false);
  await flush();
  assert.equal(toggle.checked, false);
});

test('denied grant restores actual state and displays error', async () => {
  const { elements, permissions } = popup();
  await flush();
  permissions.request = () => Promise.resolve(false);
  elements['capture-toggle'].checked = true;
  elements['capture-toggle'].change();
  await flush();
  assert.equal(elements['capture-toggle'].checked, false);
  assert.equal(elements.error.hidden, false);
});

test('existing grant defaults off until explicitly enabled', async () => {
  const { elements, calls } = popup(true);
  await flush();
  assert.equal(elements['capture-toggle'].checked, false);
  assert.equal(calls.some(call => call[0] === 'request'), false);
});
