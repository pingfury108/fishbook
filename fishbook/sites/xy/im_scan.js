// IM 页：扫描会话列表（未读 / 预览 / 激活态）
(() => {
  const TIME_RE = /^(\d+分钟前|\d+小时前|\d+天前|昨天|刚刚|\d{1,2}-\d{1,2}|\d{4}-\d{1,2}-\d{1,2})$/;
  const leaves = (root) =>
    [...root.querySelectorAll("*")].filter((n) => n.children.length === 0 && n.textContent.trim());

  const items = [...document.querySelectorAll('[class*="conversation-item"]')].filter(
    (el) => !el.className.includes("skeleton") && el.getBoundingClientRect().height > 0
  );

  return {
    url: location.href,
    conversations: items.map((el, idx) => {
      const img = el.querySelector("img");
      const badge = el.querySelector(".ant-badge-count");
      const unread = badge ? parseInt(badge.textContent, 10) || 1 : 0;
      const parts = leaves(el)
        .map((n) => n.textContent.trim())
        .filter((t) => t !== (badge ? badge.textContent : "") && !TIME_RE.test(t));
      return {
        idx: idx,
        avatar: img ? img.src : "",
        name: parts[0] || "",
        // 有的会话行会在昵称和消息之间插入商品标题，全部保留给 Python 侧匹配
        extras: parts.slice(1, 4).map((t) => t.slice(0, 60)),
        preview: (parts[parts.length - 1] || "").slice(0, 60),
        unread: unread,
        active: el.className.includes("active"),
      };
    }),
  };
})()
