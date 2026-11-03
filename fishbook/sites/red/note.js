// 小红书笔记详情：作用域锁死在 #noteContainer，避免串到右侧推荐流
(() => {
  const root = document.querySelector('#noteContainer');
  if (!root) return { error: 'NO_NOTE_CONTAINER' };

  const text = (selector) => {
    const el = root.querySelector(selector);
    return el && el.textContent ? el.textContent.trim() : null;
  };

  const interact = root.querySelector('.interact-container');
  const count = (selector) => {
    const el = interact ? interact.querySelector(selector) : null;
    return el && el.textContent ? el.textContent.trim() : null;
  };

  const body = document.body ? document.body.innerText.slice(0, 4000) : '';

  return {
    title: text('#detail-title'),
    desc: text('#detail-desc'),
    author: text('.username'),
    likes_text: count('.like-wrapper .count'),
    collects_text: count('.collect-wrapper .count'),
    comments_text: count('.chat-wrapper .count'),
    tags: [...root.querySelectorAll('#detail-desc a.tag')]
      .map((tag) => (tag.textContent || '').trim().replace(/^#/, ''))
      .filter(Boolean),
    images: [...root.querySelectorAll('.media-container img, .swiper-slide img')]
      .map((img) => img.src)
      .filter(Boolean),
    is_video: !!root.querySelector('.player-container, video'),
    not_found: /笔记不存在|当前笔记暂时无法浏览|你访问的页面不见了/.test(body),
    login_wall: /登录后查看|请登录/.test(body)
  };
})()
