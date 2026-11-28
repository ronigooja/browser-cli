// Fixed main-document operations. All dependencies are inside readPage so
// the function can be serialized for execution in a page's isolated world.
function readPage(options) {
  const { selector, waitForSelector, includeLinks = false, kind } = options;
  const expectedURL = options.expectedURL ?? options.expectedUrl;
  const hasSelector = selector !== undefined;
  const hasWaitSelector = waitForSelector !== undefined;
  function challengeState() {
    function visible(e) {
      if (e.closest(".grecaptcha-badge, [aria-hidden=\"true\"]")) return false;
      const s = getComputedStyle(e), r = e.getBoundingClientRect();
      return s.display !== "none" && s.visibility !== "hidden" && s.visibility !== "collapse" && Number(s.opacity) !== 0 && r.width > 0 && r.height > 0;
    }
    const title = document.title.trim();
    if (/attention required/i.test(title)) return "needs-human";
    const widgets = document.querySelectorAll("iframe[src], .g-recaptcha, .cf-turnstile, .h-captcha");
    for (let i = 0; i < widgets.length; i++) {
      const e = widgets[i];
      if (!visible(e)) continue;
      if (e.tagName !== "IFRAME" || /challenges\.cloudflare\.com|google\.com\/recaptcha|recaptcha\.net|hcaptcha\.com/i.test(e.getAttribute("src") || "")) return "needs-human";
    }
    if (/^just a moment(?:\.\.\.|…)?$/i.test(title) || document.querySelector("#challenge-running, #cf-challenge-running") || (document.querySelector(".main-wrapper") && document.querySelector("[id^=cf-], [class*=cf-]"))) return "cloudflare-passive";
    return "none";
  }
  function loginGate(body) {
    const route = /(?:^|\/)(?:login|log-in|signin|sign-in)(?:\/|$)/i;
    const signIn = /^(?:(?:please )?(?:log[ -]?in|sign[ -]?in)|(?:请|立即|去|马上|现在)?(?:扫码|手机(?:号)?|账号|密码)?登录(?:\s*[\/／]\s*注册)?)[.!。！]?$/i;
    const accessRequired = /^(?:(?:please )?(?:log[ -]?in|sign[ -]?in) (?:to (?:continue|(?:see|view|read|access)\b.{1,64})|first)|(?:you must|you need to) (?:log[ -]?in|sign[ -]?in) to (?:continue|(?:see|view|read|access)\b.{1,64})|(?:请(?:先)?|需要先|必须先)?登录后(?:才(?:能|可))?(?:继续|查看|浏览|阅读|访问)(?:.{0,24})|(?:请|需|必须)先登录)[.!。！]?$/i;
    if (route.test(location.pathname)) return true;
    if (!body) return false;

    function normalize(text) { return (text || "").replace(/\s+/g, " ").trim(); }
    const dialogs = body.querySelectorAll("dialog, [role=dialog], [role=alertdialog]");
    if (Array.prototype.some.call(dialogs, function(dialog) {
      if (!visible(dialog)) return false;
      const heading = dialog.querySelector("h1,h2,h3,[role=heading]");
      const label = normalize(dialog.getAttribute("aria-label") || (heading && visible(heading) && heading.innerText));
      return signIn.test(label) || accessRequired.test(label) ||
        Array.prototype.some.call(dialog.querySelectorAll("input[type=password]"), visible);
    })) return true;

    const excluded = "header,nav,footer,aside,article,blockquote,pre,code,q,[role=banner],[role=navigation],[role=contentinfo],[role=complementary],[role=article]";
    const control = "button,a[href],input[type=submit],input[type=button],[role=button],[role=link],[tabindex],[onclick]";
    function signInControl(node) {
      if (node.closest(excluded)) return false;
      const nativeControl = node.matches(control);
      if (!nativeControl && node.childElementCount) return false;
      if (!visible(node)) return false;
      const label = normalize(node.getAttribute("aria-label") || (node.tagName === "INPUT" ? node.value : node.innerText));
      const href = node.matches("a[href]") ? node.pathname : "";
      return (signIn.test(label) || (href && route.test(href))) &&
        (nativeControl || getComputedStyle(node).cursor === "pointer");
    }
    const prompts = body.querySelectorAll("h1,h2,h3,h4,h5,h6,[role=heading],p,div,span,strong,b,label");
    return Array.prototype.some.call(prompts, function(prompt) {
      // Match a short text block, not the aggregate text of a page wrapper.
      if (prompt.closest(excluded) || prompt.querySelector("div,p,section,main,article,form,header,nav,ul,ol,h1,h2,h3,h4,h5,h6")) return false;
      if (!accessRequired.test(normalize(prompt.textContent)) || !visible(prompt) ||
          !accessRequired.test(normalize(prompt.innerText))) return false;
      for (let region = prompt.parentElement, depth = 0; region && region !== body && depth < 3;
           region = region.parentElement, depth++) {
        // Limit corroboration to a compact panel; never pair with a distant CTA.
        if (normalize(region.textContent).length > 360) break;
        if (Array.prototype.some.call(region.querySelectorAll(control + ",div,span"), function(node) {
          return !prompt.contains(node) && !node.contains(prompt) && signInControl(node);
        })) return true;
      }
      return false;
    });
  }

  if (location.href !== expectedURL || (kind && window !== window.top)) return kind ? {status:'target_changed', final_url:location.href} : {status:'target_changed', document_url:location.href};
  const challenge = challengeState();
  if (challenge === 'needs-human') return kind ? {status:'verification_required', final_url:location.href} : {status:'verification_required'};
  if (challenge === 'cloudflare-passive') return kind ? {status:'passive_challenge', final_url:location.href} : {status:'passive_challenge'};
  function visible(e) {
    const s = getComputedStyle(e), r = e.getBoundingClientRect();
    if (s.visibility !== 'visible' || r.width <= 0 || r.height <= 0 || e.closest('[hidden],[aria-hidden="true"]')) return false;
    for (let node = e; node; node = node.parentElement) {
      if (Number(getComputedStyle(node).opacity) === 0) return false;
    }
    return true;
  }
  const body = document.body;
  if (loginGate(body)) return kind ? {status:'login_required', final_url:location.href} : {status:'login_required'};
  if (!body || document.readyState === 'loading') return kind ? {status:'page_loading', final_url:location.href} : {status:'page_loading'};
  if (kind) {
    const result = (status, extra = {}) => ({status, final_url:location.href, ...extra});
    const clip = (s, n) => String(s || '').slice(0, n);
    const offset = Number.isSafeInteger(options.offset) && options.offset >= 0 ? Math.min(options.offset, 100000) : 0;
    function query(s) { try { return {nodes:Array.from(document.querySelectorAll(s))}; } catch (_) { return {error:true}; } }
    function safeUrl(value) {
      try { const u = new URL(value); return /^https?:$/.test(u.protocol) && !u.username && !u.password && u.href.length <= 2048; } catch (_) { return false; }
    }
    if (kind === 'info') return result('ok', {title:clip(document.title,500), ready_state:document.readyState, challenge:'none'});
    function scroll(target) {
      if (options.direction === 'top' || options.direction === 'bottom') {
        const bottom = target === window ? document.scrollingElement?.scrollHeight : target.scrollHeight;
        target.scrollTo({top:options.direction === 'top' ? 0 : bottom, behavior:'instant'});
      } else if (options.direction === 'by' && Number.isInteger(options.pixels)) {
        target.scrollBy(0,options.pixels);
      } else return result('action_unavailable');
      return result('ok');
    }
    if (kind === 'snapshot') {
      const key = '__agentBrowserPageOperationRefs';
      let store = globalThis[key];
      if (!store || store.document !== document || Date.now() - store.created > 300000) {
        store = {document, created:Date.now(), refs:new Map()};
        Object.defineProperty(globalThis, key, {value:store, configurable:true});
      }
      const found = query(selector || 'a[href],button,input,select,textarea,[role="button"],[role="link"],[tabindex]');
      if (found.error) return result('invalid_selector');
      const nodes = found.nodes.filter(visible);
      const elements = [];
      for (const node of nodes.slice(offset, offset + 25)) {
        const bytes = new Uint8Array(12); crypto.getRandomValues(bytes);
        const ref = Array.from(bytes, b => b.toString(16).padStart(2,'0')).join('');
        if (store.refs.size >= 500) store.refs.delete(store.refs.keys().next().value);
        store.refs.set(ref, node);
        const href = node.matches('a[href]') && safeUrl(node.href) ? node.href : null;
        elements.push({ref, tag:clip(node.tagName.toLowerCase(),32), text:clip(node.getAttribute('aria-label') || node.innerText || '',200),
          ...(href && href.length <= 200 ? {url:href} : {})});
      }
      return result('ok', {elements, next_offset:offset + elements.length < nodes.length ? offset + elements.length : null});
    }
    if (kind === 'wait') {
      const condition = options.condition || 'visible';
      if (!['visible','hidden','text'].includes(condition) || (condition !== 'text' && !selector) || (condition === 'text' && typeof options.text !== 'string')) return result('action_unavailable');
      const found = selector ? query(selector) : {nodes:[body]};
      if (found.error) return result('invalid_selector');
      const matching = found.nodes.filter(visible);
      const ready = condition === 'hidden' ? matching.length === 0 : condition === 'visible' ? matching.length > 0 : matching.some(e => (e.innerText || '').includes(options.text));
      return result(ready ? 'ok' : 'content_not_ready');
    }
    if (kind === 'html' || kind === 'extract') {
      const found = selector ? query(selector) : {nodes:[body]};
      if (found.error) return result('invalid_selector');
      const root = found.nodes.find(visible);
      if (!root || root.matches('script,style,noscript')) return result('no_match');
      if (kind === 'html') {
        let budget = 9000, truncated = false;
        const parts = [];
        const escape = value => value.replace(/[&<>]/g,c => ({'&':'&amp;','<':'&lt;','>':'&gt;'}[c]));
        const passiveTags = /^(?:a|article|aside|b|blockquote|br|caption|code|dd|div|dl|dt|em|figcaption|figure|footer|h[1-6]|header|hr|i|li|main|nav|ol|p|pre|section|small|span|strong|table|tbody|td|th|thead|tr|ul)$/;
        function append(text) {
          if (text.length > budget) { truncated = true; return false; }
          parts.push(text); budget -= text.length; return true;
        }
        function walk(node, depth) {
          if (depth > 50 || truncated) { truncated = true; return; }
          const tag = node.tagName.toLowerCase();
          if (!visible(node) || /^(?:script|style|noscript|template|iframe|textarea|input|select)$/.test(tag) || node.isContentEditable) return;
          const start = passiveTags.test(tag) ? `<${tag}>` : '';
          const end = start && tag !== 'br' && tag !== 'hr' ? `</${tag}>` : '';
          if (budget < start.length + end.length) { truncated = true; return; }
          budget -= end.length;
          if (start) append(start);
          for (const child of node.childNodes) {
            if (truncated) break;
            if (child.nodeType === 1) walk(child,depth + 1);
            if (child.nodeType === 3 && child.nodeValue) append(escape(child.nodeValue.slice(0,budget+1)));
          }
          parts.push(end);
        }
        if (options.raw === true) return result('action_unavailable');
        walk(root,0);
        return result('ok', {html:parts.join(''),truncated});
      }
      const text = root.innerText || '';
      const anchors = Array.from(root.querySelectorAll('a[href]'));
      if (root.matches('a[href]')) anchors.unshift(root);
      const eligible = anchors.filter(a => visible(a) && safeUrl(a.href));
      const links = [];
      let budget = 6000;
      for (const a of eligible.slice(offset,offset + 25)) {
        const label = clip((a.innerText || '').trim(),200);
        if (a.href.length + label.length > budget) break;
        budget -= a.href.length + label.length;
        links.push({text:label,url:a.href});
      }
      const next_offset = offset + links.length < eligible.length ? offset + links.length : null;
      return result('ok', {text:offset === 0 ? text.slice(0,4000) : '', links, next_offset, truncated:next_offset !== null || text.length > 4000});
    }
    if (kind === 'scroll' && !selector && !options.elementRef) return scroll(window);
    let target;
    if (options.elementRef) {
      const store = globalThis.__agentBrowserPageOperationRefs;
      target = store && store.document === document && Date.now() - store.created <= 300000 && store.refs.get(options.elementRef);
      if (!target || !target.isConnected || target.ownerDocument !== document || !visible(target)) return result('stale_element');
    } else if (selector) {
      const found = query(selector);
      if (found.error) return result('invalid_selector');
      target = found.nodes.find(visible);
      if (!target) return result('no_match');
    } else if (kind === 'press') target = document.activeElement;
    else return result('action_unavailable');
    if (!target) return result('action_unavailable');
    if (target.matches(':disabled,[aria-disabled="true"]')) return result('action_unavailable');
    if (kind === 'click') {
      // A click must not implicitly submit a form.
      if (target.closest('button:not([type]),button[type="submit"],input[type="submit"],input[type="image"]')) return result('action_unavailable');
      const anchor = target.closest('a[href]');
      if (anchor && (!safeUrl(anchor.href) || anchor.hasAttribute('download') ||
          !['', '_self'].includes((anchor.target || document.querySelector('base[target]')?.target || '').toLowerCase()))) {
        return result('action_unavailable');
      }
      target.click();
    } else if (kind === 'fill') {
      if (target.readOnly || typeof options.text !== 'string' || options.text.length > 4000) return result('action_unavailable');
      if (target.matches('textarea,input:not([type]),input[type="text"],input[type="search"],input[type="email"],input[type="url"],input[type="tel"]')) {
        target.value = options.text;
      } else if (target.isContentEditable && target.hasAttribute('contenteditable')) {
        target.textContent = options.text;
      } else return result('action_unavailable');
      target.dispatchEvent(new Event('input', {bubbles:true}));
      target.dispatchEvent(new Event('change', {bubbles:true}));
    } else if (kind === 'select') {
      if (target.tagName !== 'SELECT' || typeof options.value !== 'string') return result('action_unavailable');
      const choices = Array.from(target.options).filter(o => !o.matches(':disabled'));
      const option = choices.find(o => o.value === options.value) || choices.find(o => o.label === options.value);
      if (!option) return result('action_unavailable');
      target.selectedIndex = option.index;
      target.dispatchEvent(new Event('input', {bubbles:true}));
      target.dispatchEvent(new Event('change', {bubbles:true}));
    } else if (kind === 'press') {
      // Synthetic keyboard events do not invoke browser default actions.
      if (!['Enter','Tab','Escape','Backspace','ArrowUp','ArrowDown','ArrowLeft','ArrowRight'].includes(options.key) &&
          !(typeof options.key === 'string' && [...options.key].length === 1)) return result('action_unavailable');
      target.dispatchEvent(new KeyboardEvent('keydown', {key:options.key,bubbles:true}));
      target.dispatchEvent(new KeyboardEvent('keyup', {key:options.key,bubbles:true}));
    } else if (kind === 'submit') {
      const form = target.tagName === 'FORM' ? target : target.form;
      if (!form || typeof form.requestSubmit !== 'function') return result('action_unavailable');
      form.requestSubmit();
    } else if (kind === 'scroll') return scroll(target);
    else return result('action_unavailable');
    return result('ok');
  }
  if (hasWaitSelector) {
    try {
      const matches = document.querySelectorAll(waitForSelector);
      if (!Array.prototype.some.call(matches, visible)) return {status:'content_not_ready'};
    } catch (e) {
      if (e.name === 'SyntaxError') return {status:'invalid_wait_selector'};
      throw e;
    }
  }
  let root = body;
  if (hasSelector) {
    try { root = document.querySelector(selector); }
    catch (e) {
      if (e.name === 'SyntaxError') return {status:'invalid_selector'};
      throw e;
    }
    if (!root) return {status:'no_match'};
    if (root.matches('script,style,noscript')) return {status:'no_content'};
  }
  // innerText can fall back to hidden DOM text when its root is not rendered.
  if (!visible(root)) return {status:'no_content'};
  const text = root.innerText || '';
  if (!text.trim()) return {status:'no_content'};
  function safeUrl(value) {
    try { const u = new URL(value); return /^https?:$/.test(u.protocol) && !u.username && !u.password && u.href.length <= 2048; }
    catch (_) { return false; }
  }
  const result = {status:'ok', title:document.title.slice(0,500), final_url:location.href,
                text:text.slice(0,12000), truncated:text.length > 12000 || document.title.length > 500};
  if (includeLinks) {
    result.links = [];
    let budget = 8000;
    const anchors = Array.from(root.querySelectorAll('a[href]'));
    if (hasSelector && root.matches('a[href]')) anchors.unshift(root);
    for (let i = 0; i < anchors.length; i++) {
      const a = anchors[i];
      if (!visible(a) || !safeUrl(a.href)) continue;
      const label = (a.innerText || '').trim();
      budget -= Math.min(label.length,200) + a.href.length;
      if (result.links.length === 25 || budget < 0) { result.truncated = true; break; }
      if (label.length > 200) result.truncated = true;
      result.links.push({text:label.slice(0,200), url:a.href});
    }
  }
  return result;
}

var agentBrowserPageOperation = readPage;
if (typeof module !== "undefined" && module.exports) module.exports = { readPage, agentBrowserPageOperation };
