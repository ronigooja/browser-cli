const test = require('node:test');
const assert = require('node:assert/strict');
const vm = require('node:vm');
const fs = require('node:fs');
const path = require('node:path');
const { readPage } = require('../extension/shared/read-page.js');

function element(options = {}) {
  return {
    tagName: 'DIV', parentElement: null, childElementCount: 0,
    innerText: '', textContent: '', style: {}, ...options,
    closest() { return this.hidden ? this : null; },
    getBoundingClientRect() { return { width: 10, height: 10 }; },
    getAttribute(name) { return this.attributes?.[name] ?? null; },
    matches(selector) { return selector === 'a[href]' && this.tagName === 'A'; },
    querySelector() { return null; },
    querySelectorAll(selector) { return selector === 'a[href]' ? (this.anchors || []) : []; },
  };
}

function probe(options = {}) {
  const body = options.body === undefined ? element({ innerText: 'Hello' }) : options.body;
  const calls = [];
  const loc = new URL(options.url || 'https://example.com/');
  const document = {
    title: options.title || 'Title', body, readyState: options.readyState || 'complete',
    querySelector(selector) {
      calls.push(selector);
      if (selector === options.badSelector) throw new SyntaxError();
      return options.selectors?.[selector] || null;
    },
    querySelectorAll(selector) {
      calls.push(selector);
      if (selector === options.badSelector) throw new SyntaxError();
      if (selector.startsWith('iframe[src]')) return options.widgets || [];
      return options.all?.[selector] || [];
    },
  };
  const context = { document, location: loc, URL, getComputedStyle: e => ({
    visibility: 'visible', display: 'block', opacity: '1', ...e.style,
  }) };
  // The function's source is evaluated as a classic script in a fresh realm,
  // proving it does not rely on its CommonJS module or surrounding closure.
  const isolated = vm.runInNewContext(`(${readPage.toString()})`, context);
  return { run: args => JSON.parse(JSON.stringify(isolated(args))), calls, document };
}

test('classic script defines a global and CommonJS exports the function', () => {
  const source = fs.readFileSync(path.join(__dirname, '../extension/shared/read-page.js'), 'utf8');
  const context = vm.createContext({});
  vm.runInContext(source, context);
  assert.equal(typeof context.readPage, 'function');
  assert.equal(typeof readPage, 'function');
});

test('serialized function reads without module-scope dependencies', () => {
  const p = probe({ body: element({ innerText: 'isolated' }) });
  assert.equal(p.run({ expectedURL: 'https://example.com/' }).text, 'isolated');
});

test('URL guard runs before any document access', () => {
  const context = { location: { href: 'https://other/' }, document: new Proxy({}, {
    get() { throw new Error('unexpected DOM access'); },
  }) };
  const isolated = vm.runInNewContext(`(${readPage.toString()})`, context);
  assert.equal(isolated({ expectedURL: 'https://example.com/' }).status, 'target_changed');
});

test('challenge and login blockers precede extraction', () => {
  const body = element();
  Object.defineProperty(body, 'innerText', { get() { throw new Error('extracted'); } });
  const args = { expectedURL: 'https://example.com/' };
  assert.equal(probe({ title: 'Attention Required', body }).run(args).status, 'verification_required');
  assert.equal(probe({ title: 'Just a moment...', body }).run(args).status, 'passive_challenge');
  assert.equal(probe({ widgets: [element({ tagName: 'DIV' })], body }).run(args).status, 'verification_required');
  assert.equal(probe({ url: 'https://example.com/login', body }).run({ expectedURL: 'https://example.com/login' }).status, 'login_required');
});

test('selector, wait selector and links use the received arguments', () => {
  const anchor = element({ tagName: 'A', href: 'https://example.com/link', innerText: 'Link' });
  const root = element({ innerText: 'Scoped', anchors: [anchor] });
  const p = probe({ selectors: { '#scope': root }, all: { '#ready': [element()] } });
  assert.deepEqual(p.run({ expectedURL: 'https://example.com/', selector: '#scope', waitForSelector: '#ready', includeLinks: true }), {
    status: 'ok', title: 'Title', final_url: 'https://example.com/', text: 'Scoped', truncated: false,
    links: [{ text: 'Link', url: 'https://example.com/link' }],
  });
  assert.ok(p.calls.includes('#scope'));
  assert.ok(p.calls.includes('#ready'));
  assert.equal(probe().run({ expectedURL: 'https://example.com/', waitForSelector: '#missing' }).status, 'content_not_ready');
  assert.equal(probe({ badSelector: '[' }).run({ expectedURL: 'https://example.com/', selector: '[' }).status, 'invalid_selector');
  assert.equal(probe({ badSelector: '[' }).run({ expectedURL: 'https://example.com/', waitForSelector: '[' }).status, 'invalid_wait_selector');
});

test('hidden full body never exposes innerText', () => {
  const body = element({ hidden: true });
  Object.defineProperty(body, 'innerText', { get() { throw new Error('hidden text leaked'); } });
  assert.equal(probe({ body }).run({ expectedURL: 'https://example.com/' }).status, 'no_content');
});

test('caps use UTF-16 units and filter links', () => {
  const anchors = Array.from({ length: 26 }, (_, i) => element({ tagName: 'A', href: `https://example.com/${i}`, innerText: '😀'.repeat(101) }));
  anchors.unshift(element({ tagName: 'A', href: 'javascript:alert(1)', innerText: 'bad' }));
  const body = element({ innerText: '😀'.repeat(6001), anchors });
  const result = probe({ body, title: '😀'.repeat(251) }).run({ expectedURL: 'https://example.com/', includeLinks: true });
  assert.equal(result.text.length, 12000);
  assert.equal(result.title.length, 500);
  assert.equal(result.links.length, 25);
  assert.equal(result.links[0].text.length, 200);
  assert.equal(result.truncated, true);
});
