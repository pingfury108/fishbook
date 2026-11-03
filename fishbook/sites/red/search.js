// 小红书搜索：输入 → 点击 → 等结果 → 滚动收割（全程在页面内，一次 evaluate 干完）
// 占位符：__KEYWORD__ __LIMIT__ __MAX_SCROLLS__
(async () => {
  const KEYWORD = __KEYWORD__;
  const LIMIT = __LIMIT__;
  const MAX_SCROLLS = __MAX_SCROLLS__;
  const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

  // 可见性三层判定：尺寸非零 + 逐级 display/visibility/opacity + 排除 hidden 祖先
  const visible = (el) => {
    const rect = el.getBoundingClientRect();
    if (!rect.width || !rect.height) return false;
    for (let node = el; node && node.nodeType === 1; node = node.parentElement) {
      if (node.hasAttribute('hidden') || node.hasAttribute('inert')) return false;
      if (node.getAttribute('aria-hidden') === 'true') return false;
      const style = getComputedStyle(node);
      if (style.display === 'none' || style.visibility === 'hidden') return false;
      if (parseFloat(style.opacity) === 0) return false;
    }
    return true;
  };

  const unique = (selector) => {
    const nodes = [...document.querySelectorAll(selector)].filter(visible);
    return nodes.length === 1 ? nodes[0] : null;
  };

  const waitFor = async (fn, ms) => {
    const start = Date.now();
    while (Date.now() - start < ms) {
      const value = fn();
      if (value) return value;
      await sleep(200);
    }
    return null;
  };

  const riskHit = () => {
    const url = location.href;
    const text = document.body ? document.body.innerText.slice(0, 4000) : '';
    const urlMarkers = ['/website-login/error', 'error_code=300017', 'error_code=300031', '/website-login/captcha'];
    const textMarkers = ['安全限制', '访问链接异常', '请求太频繁', '访问频次异常'];
    for (const marker of urlMarkers) if (url.includes(marker)) return marker;
    for (const marker of textMarkers) if (text.includes(marker)) return marker;
    return null;
  };

  const pick = (card, selectors) => {
    for (const selector of selectors) {
      const el = card.querySelector(selector);
      const text = el && el.textContent ? el.textContent.trim() : '';
      if (text) return text;
    }
    return null;
  };

  const extract = () => {
    const cards = [...document.querySelectorAll('section.note-item')];
    const out = [];
    for (const card of cards) {
      const link = card.querySelector(
        'a[href*="/search_result/"], a[href*="/explore/"], a[href*="/note/"]'
      );
      const href = link ? link.href : null;
      if (!href) continue;
      const matched = href.match(/\/(?:search_result|explore|note)\/([0-9a-f]{24})/);
      if (!matched) continue;
      out.push({
        note_id: matched[1],
        note_url: href,
        title: pick(card, ['a.title span', '.footer .title', 'a.title', '.title']),
        author: pick(card, ['.author .name', 'a.author span', '.author']),
        likes_text: pick(card, ['.like-wrapper .count', '.count']),
        is_video: !!card.querySelector('.play-icon, .video-icon, .play')
      });
    }
    return out;
  };

  // 1) 输入框必须唯一且可见
  const input = await waitFor(() => unique('#search-input'), 10000);
  if (!input) return { error: 'NO_SEARCH_INPUT' };

  // 2) 原生 setter 赋值 + input/change，回读校验
  const setter = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value').set;
  setter.call(input, KEYWORD);
  input.dispatchEvent(new Event('input', { bubbles: true }));
  input.dispatchEvent(new Event('change', { bubbles: true }));
  if (input.value !== KEYWORD) return { error: 'INPUT_SET_FAILED' };

  // 3) 搜索按钮必须唯一
  const icon = await waitFor(() => unique('.search-icon'), 5000);
  if (!icon) return { error: 'NO_SEARCH_ICON' };
  icon.click();

  // 4) 等路由与结果
  const state = await waitFor(() => {
    const keyword = new URLSearchParams(location.search).get('keyword') || '';
    if (location.pathname !== '/search_result' || keyword !== KEYWORD) return null;
    if (document.querySelector('section.note-item')) return 'cards';
    if (document.querySelector('.search-empty-wrapper')) return 'empty';
    if (/登录后查看|请登录/.test(document.body ? document.body.innerText : '')) return 'login';
    return null;
  }, 15000);

  if (!state) return { error: 'SEARCH_TIMEOUT', risk: riskHit() };
  if (state === 'login') return { error: 'LOGIN_REQUIRED' };
  if (state === 'empty') return { cards: [] };

  // 5) 滚动收割：连续 2 轮无新增即停
  const seen = new Map();
  let idle = 0;
  for (let i = 0; i < MAX_SCROLLS && seen.size < LIMIT; i++) {
    const before = seen.size;
    for (const card of extract()) if (!seen.has(card.note_id)) seen.set(card.note_id, card);
    if (seen.size >= LIMIT) break;
    idle = seen.size === before ? idle + 1 : 0;
    if (idle >= 2) break;
    window.scrollTo(0, document.body.scrollHeight);
    await sleep(1000 + Math.random() * 1000);
  }
  for (const card of extract()) if (!seen.has(card.note_id)) seen.set(card.note_id, card);

  return { cards: [...seen.values()].slice(0, LIMIT), risk: riskHit() };
})()
