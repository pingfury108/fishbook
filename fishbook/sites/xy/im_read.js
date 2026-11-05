// IM 页：读取当前会话的消息流 + 会话头商品上下文
(() => {
  const LIMIT = __LIMIT__;
  const sc = document.querySelector('[class*="scroll-container"]');
  const panel = document.querySelector('[class*="chat"]');

  const msgs = sc
    ? [...sc.querySelectorAll('[class*="message-text"]')].slice(-LIMIT).map((el) => ({
        side: el.className.includes("right") ? "me" : "other",
        text: (el.textContent || "").trim().slice(0, 500),
      }))
    : [];

  // 会话头部商品上下文（标题/价格），找不到就空
  let product = null;
  const headerEl = document.querySelector('[class*="header"] [class*="product"], [class*="product-card"], [class*="goods"]');
  if (headerEl && headerEl.getBoundingClientRect().height > 0) {
    product = { text: headerEl.textContent.trim().slice(0, 120) };
  }

  return {
    messages: msgs,
    product: product,
    inputExists: !!document.querySelector("textarea.ant-input"),
    activeName: (() => {
      const item = document.querySelector('[class*="conversation-item-active"]');
      return item ? item.textContent.trim().slice(0, 20) : null;
    })(),
    panelText: panel ? panel.textContent.trim().slice(0, 100) : null,
  };
})()
