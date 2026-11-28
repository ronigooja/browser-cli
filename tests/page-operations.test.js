const test = require('node:test');
const assert = require('node:assert/strict');
const vm = require('node:vm');
const { webcrypto } = require('node:crypto');
const { agentBrowserPageOperation } = require('../extension/shared/read-page.js');

function setup() {
  const node = {
    tagName:'BUTTON', innerText:'Go', isConnected:true, parentElement:null,
    closest(s) { return this.submit && s.startsWith('button:not') ? this : null; }, getAttribute() { return null; },
    getBoundingClientRect() { return {width:10,height:10}; },
    matches(s) { return s === 'button:not([type]),button[type="submit"],input[type="submit"],input[type="image"]' && this.submit; },
    querySelectorAll() { return []; }, querySelector() { return null; },
    click() { this.clicked = true; },
  };
  const body = {...node, tagName:'BODY', innerText:'Hello world'};
  const document = {
    title:'Page', body, readyState:'complete',
    querySelector() { return null; },
    querySelectorAll(s) { if (s === '[') throw new SyntaxError(); if (s === '#go' || s.startsWith('a[href],button')) return [node]; return []; },
  };
  node.ownerDocument = document;
  const context = {document, location:new URL('https://example.com/'), URL, crypto:webcrypto,
    getComputedStyle:() => ({visibility:'visible',display:'block',opacity:'1'})};
  context.window = context; context.top = context;
  const op = vm.runInNewContext(`(${agentBrowserPageOperation.toString()})`, context);
  return {node, document, run: o => op({expectedUrl:'https://example.com/', ...o})};
}

test('serialized operation gates before actions', () => {
  const p = setup();
  assert.equal(p.run({kind:'click', selector:'#go', expectedUrl:'https://wrong/'}).status, 'target_changed');
  p.document.title = 'Attention Required';
  assert.equal(p.run({kind:'click', selector:'#go'}).status, 'verification_required');
  assert.equal(p.node.clicked, undefined);
});

test('snapshot refs identify the original connected element', () => {
  const p = setup();
  const ref = p.run({kind:'snapshot'}).elements[0].ref;
  assert.match(ref, /^[a-f0-9]{24}$/);
  assert.equal(p.run({kind:'click', elementRef:ref}).status, 'ok');
  p.node.isConnected = false;
  assert.equal(p.run({kind:'click', elementRef:ref}).status, 'stale_element');
});

test('wait, invalid selectors and implicit submit are constrained', () => {
  const p = setup();
  assert.equal(p.run({kind:'wait', condition:'text', text:'Hello'}).status, 'ok');
  assert.equal(p.run({kind:'wait', condition:'hidden', selector:'#missing'}).status, 'ok');
  assert.equal(p.run({kind:'click', selector:'['}).status, 'invalid_selector');
  p.node.submit = true;
  assert.equal(p.run({kind:'click', selector:'#go'}).status, 'action_unavailable');
});
