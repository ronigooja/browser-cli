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
  const body = {...node, tagName:'BODY', innerText:'Hello world', childNodes:[]};
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
  return {node, document, context, body, run: o => op({expectedURL:'https://example.com/', ...o})};
}

test('serialized operation gates before actions', () => {
  const p = setup();
  assert.equal(p.run({kind:'click', selector:'#go', expectedURL:'https://wrong/'}).status, 'target_changed');
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

test('clean HTML never serializes attributes, hidden children or scripts', () => {
  const p = setup();
  const child = (tagName, value) => ({...p.node, nodeType:1, tagName, childNodes:[{nodeType:3, nodeValue:value}]});
  p.body.childNodes = [child('P', '<b>&'), child('SCRIPT', 'secret'), child('INPUT','secret'),
    {...child('P','secret'), getBoundingClientRect:() => ({width:0,height:0})}];
  assert.equal(p.run({kind:'html'}).html, '<p>&lt;b&gt;&amp;</p>');
  assert.equal(p.run({kind:'html', raw:true}).status, 'action_unavailable');
});

test('link pagination advances independently of text length', () => {
  const p = setup();
  p.body.querySelectorAll = () => Array.from({length:28}, (_, i) => ({...p.node, href:`https://example.com/${i}`}));
  const first = p.run({kind:'extract'});
  assert.equal(first.links.length,25);
  assert.equal(first.next_offset,25);
  const second = p.run({kind:'extract', offset:first.next_offset});
  assert.equal(second.links[0].url,'https://example.com/25');
  assert.equal(second.links.length,3);
  assert.equal(second.next_offset,null);
});

test('scroll dispatches only the requested movement and does not act on verification', () => {
  const p = setup();
  const coordinates = [];
  p.context.scrollBy = (x,y) => coordinates.push([x,y]);
  assert.equal(p.run({kind:'scroll', direction:'by', pixels:-100}).status, 'ok');
  p.document.title = 'Attention Required';
  assert.equal(p.run({kind:'scroll', direction:'by', pixels:100}).status, 'verification_required');
  assert.deepEqual(coordinates, [[0,-100]]);
});

test('fill uses literal text and press dispatches only synthetic keyboard events', () => {
  const p = setup();
  const events = [];
  p.node.matches = selector => selector.startsWith('textarea,');
  p.node.dispatchEvent = event => events.push([event.type,event.key]);
  p.context.Event = p.context.KeyboardEvent = class { constructor(type, options) { Object.assign(this, {type, ...options}); } };
  assert.equal(p.run({kind:'fill', selector:'#go', text:'<b>文字</b>'}).status, 'ok');
  assert.equal(p.node.value, '<b>文字</b>');
  assert.equal(p.run({kind:'press', selector:'#go', key:'Enter'}).status, 'ok');
  assert.deepEqual(events, [['input',undefined],['change',undefined],['keydown','Enter'],['keyup','Enter']]);
});
