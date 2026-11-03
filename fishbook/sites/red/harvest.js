// 小红书搜索第 2 步：抓取当前 DOM 里的卡片（同步，单轮）
(() => {
  const pick = (card, selectors) => {
    for (const selector of selectors) {
      const el = card.querySelector(selector);
      const text = el && el.textContent ? el.textContent.trim() : '';
      if (text) return text;
    }
    return null;
  };

  // SSR state 里的 note_id → xsec_token，作为锚点没带 token 时的兜底（都是页面自己的数据）
  const unwrap = (x) =>
    x && x.__v_isRef ? (x._rawValue !== undefined ? x._rawValue : x._value) : x;
  const tokenMap = {};
  try {
    const search = window.__INITIAL_STATE__ && window.__INITIAL_STATE__.search;
    for (const feed of ((search && unwrap(search.feeds)) || []).map(unwrap)) {
      if (feed && feed.id && feed.xsecToken) tokenMap[feed.id] = feed.xsecToken;
    }
  } catch (e) {}

  const cards = [...document.querySelectorAll('section.note-item')];
  const out = [];
  for (const card of cards) {
    const links = [
      ...card.querySelectorAll('a[href*="/search_result/"], a[href*="/explore/"], a[href*="/note/"]')
    ];
    // 优先取带 xsec_token 的锚点：详情页只靠 note_id 打开会被 302 到 /404?error_code=300031
    const link =
      links.find((a) => (a.getAttribute('href') || '').includes('xsec_token')) || links[0];
    const href = link ? link.href : null;
    if (!href) continue;
    const matched = href.match(/\/(?:search_result|explore|note)\/([0-9a-f]{24})/);
    if (!matched) continue;

    let noteUrl = href;
    if (!/xsec_token=/.test(noteUrl)) {
      const token = tokenMap[matched[1]];
      if (token) {
        const glue = noteUrl.includes('?') ? '&' : '?';
        noteUrl = `${noteUrl}${glue}xsec_token=${encodeURIComponent(token)}&xsec_source=pc_search`;
      }
    }
    out.push({
      note_id: matched[1],
      note_url: noteUrl,
      title: pick(card, ['a.title span', '.footer .title', 'a.title', '.title']),
      author: pick(card, ['.author .name', 'a.author span', '.author']),
      likes_text: pick(card, ['.like-wrapper .count', '.count']),
      is_video: !!card.querySelector('.play-icon, .video-icon, .play')
    });
  }

  const url = location.href;
  const text = document.body ? document.body.innerText.slice(0, 4000) : '';
  const urlMarkers = ['/website-login/error', 'error_code=300017', 'error_code=300031', '/website-login/captcha'];
  const textMarkers = ['安全限制', '访问链接异常', '请求太频繁', '访问频次异常'];
  let risk = null;
  for (const marker of urlMarkers) if (!risk && url.includes(marker)) risk = marker;
  for (const marker of textMarkers) if (!risk && text.includes(marker)) risk = marker;

  const reachedBottom =
    window.innerHeight + window.scrollY >= document.documentElement.scrollHeight - 200;

  return {
    cards: out,
    risk: risk,
    loginWall: /登录后查看|请登录/.test(text),
    path: location.pathname,
    keyword: new URLSearchParams(location.search).get('keyword') || '',
    empty: !!document.querySelector('.search-empty-wrapper'),
    reachedBottom: reachedBottom
  };
})()
