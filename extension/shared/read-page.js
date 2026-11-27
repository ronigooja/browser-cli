// Fixed, read-only main-document probe. All dependencies are inside readPage so
// the function can be serialized for execution in a page's isolated world.
function readPage({ expectedURL, selector, waitForSelector, includeLinks = false }) {
  const hasSelector = selector !== undefined;
  const hasWaitSelector = waitForSelector !== undefined;
function challengeState() {
  function visible(e) {
    if (e.closest(".grecaptcha-badge, [aria-hidden=\"true\"]")) return false;
    var s = getComputedStyle(e), r = e.getBoundingClientRect();
    return s.display !== "none" && s.visibility !== "hidden" && s.visibility !== "collapse" && Number(s.opacity) !== 0 && r.width > 0 && r.height > 0;
  }
  var title = document.title.trim();
  if (/attention required/i.test(title)) return "needs-human";
  var widgets = document.querySelectorAll("iframe[src], .g-recaptcha, .cf-turnstile, .h-captcha");
  for (var i = 0; i < widgets.length; i++) {
    var e = widgets[i];
    if (!visible(e)) continue;
    if (e.tagName !== "IFRAME" || /challenges\.cloudflare\.com|google\.com\/recaptcha|recaptcha\.net|hcaptcha\.com/i.test(e.getAttribute("src") || "")) return "needs-human";
  }
  if (/^just a moment(?:\.\.\.|…)?$/i.test(title) || document.querySelector("#challenge-running, #cf-challenge-running") || (document.querySelector(".main-wrapper") && document.querySelector("[id^=cf-], [class*=cf-]"))) return "cloudflare-passive";
  return "none";
}
function loginGate(body) {
  var route = /(?:^|\/)(?:login|log-in|signin|sign-in)(?:\/|$)/i;
  var signIn = /^(?:(?:please )?(?:log[ -]?in|sign[ -]?in)|(?:请|立即|去|马上|现在)?(?:扫码|手机(?:号)?|账号|密码)?登录(?:\s*[\/／]\s*注册)?)[.!。！]?$/i;
  var accessRequired = /^(?:(?:please )?(?:log[ -]?in|sign[ -]?in) (?:to (?:continue|(?:see|view|read|access)\b.{1,64})|first)|(?:you must|you need to) (?:log[ -]?in|sign[ -]?in) to (?:continue|(?:see|view|read|access)\b.{1,64})|(?:请(?:先)?|需要先|必须先)?登录后(?:才(?:能|可))?(?:继续|查看|浏览|阅读|访问)(?:.{0,24})|(?:请|需|必须)先登录)[.!。！]?$/i;
  if (route.test(location.pathname)) return true;
  if (!body) return false;

  function normalize(text) { return (text || "").replace(/\s+/g, " ").trim(); }
  var dialogs = body.querySelectorAll("dialog, [role=dialog], [role=alertdialog]");
  if (Array.prototype.some.call(dialogs, function(dialog) {
    if (!visible(dialog)) return false;
    var heading = dialog.querySelector("h1,h2,h3,[role=heading]");
    var label = normalize(dialog.getAttribute("aria-label") || (heading && visible(heading) && heading.innerText));
    return signIn.test(label) || accessRequired.test(label) ||
      Array.prototype.some.call(dialog.querySelectorAll("input[type=password]"), visible);
  })) return true;

  var excluded = "header,nav,footer,aside,article,blockquote,pre,code,q,[role=banner],[role=navigation],[role=contentinfo],[role=complementary],[role=article]";
  var control = "button,a[href],input[type=submit],input[type=button],[role=button],[role=link],[tabindex],[onclick]";
  function signInControl(node) {
    if (node.closest(excluded)) return false;
    var nativeControl = node.matches(control);
    if (!nativeControl && node.childElementCount) return false;
    if (!visible(node)) return false;
    var label = normalize(node.getAttribute("aria-label") || (node.tagName === "INPUT" ? node.value : node.innerText));
    var href = node.matches("a[href]") ? node.pathname : "";
    return (signIn.test(label) || (href && route.test(href))) &&
      (nativeControl || getComputedStyle(node).cursor === "pointer");
  }
  var prompts = body.querySelectorAll("h1,h2,h3,h4,h5,h6,[role=heading],p,div,span,strong,b,label");
  return Array.prototype.some.call(prompts, function(prompt) {
    // Match a short text block, not the aggregate text of a page wrapper.
    if (prompt.closest(excluded) || prompt.querySelector("div,p,section,main,article,form,header,nav,ul,ol,h1,h2,h3,h4,h5,h6")) return false;
    if (!accessRequired.test(normalize(prompt.textContent)) || !visible(prompt) ||
        !accessRequired.test(normalize(prompt.innerText))) return false;
    for (var region = prompt.parentElement, depth = 0; region && region !== body && depth < 3;
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

    if (location.href !== expectedURL) return {status:'target_changed', document_url:location.href};
    var challenge = challengeState();
    if (challenge === 'needs-human') return {status:'verification_required'};
    if (challenge === 'cloudflare-passive') return {status:'passive_challenge'};
    function visible(e) {
      var s = getComputedStyle(e), r = e.getBoundingClientRect();
      if (s.visibility !== 'visible' || r.width <= 0 || r.height <= 0 || e.closest('[hidden],[aria-hidden="true"]')) return false;
      for (var node = e; node; node = node.parentElement) {
        if (Number(getComputedStyle(node).opacity) === 0) return false;
      }
      return true;
    }
    var body = document.body;
    if (loginGate(body)) return {status:'login_required'};
    if (!body || document.readyState === 'loading') return {status:'page_loading'};
    if (hasWaitSelector) {
      try {
        var matches = document.querySelectorAll(waitForSelector);
        if (!Array.prototype.some.call(matches, visible)) return {status:'content_not_ready'};
      } catch (e) {
        if (e.name === 'SyntaxError') return {status:'invalid_wait_selector'};
        throw e;
      }
    }
    var root = body;
    if (hasSelector) {
      try { root = document.querySelector(selector); }
      catch (e) {
        if (e.name === 'SyntaxError') return {status:'invalid_selector'};
        throw e;
      }
      if (!root) return {status:'no_match'};
      // innerText can fall back to hidden DOM text when its root is not rendered.
      if (root.matches('script,style,noscript') || !visible(root)) return {status:'no_content'};
    }
    if (!visible(root)) return {status:'no_content'};
    var text = root.innerText || '';
    if (!text.trim()) return {status:'no_content'};
    function safeUrl(value) {
      try { var u = new URL(value); return /^https?:$/.test(u.protocol) && !u.username && !u.password && u.href.length <= 2048; }
      catch (_) { return false; }
    }
    var result = {status:'ok', title:document.title.slice(0,500), final_url:location.href,
                  text:text.slice(0,12000), truncated:text.length > 12000 || document.title.length > 500};
    if (includeLinks) {
      result.links = [];
      var budget = 8000;
      var anchors = Array.from(root.querySelectorAll('a[href]'));
      if (hasSelector && root.matches('a[href]')) anchors.unshift(root);
      for (var i = 0; i < anchors.length; i++) {
        var a = anchors[i];
        if (!visible(a) || !safeUrl(a.href)) continue;
        var label = (a.innerText || '').trim();
        budget -= Math.min(label.length,200) + a.href.length;
        if (result.links.length === 25 || budget < 0) { result.truncated = true; break; }
        if (label.length > 200) result.truncated = true;
        result.links.push({text:label.slice(0,200), url:a.href});
      }
    }
    return result;
}

if (typeof module !== "undefined" && module.exports) module.exports = { readPage };
