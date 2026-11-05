// IM 页：按买家昵称点开会话
(() => {
  const NAME = __NAME__;
  const items = [...document.querySelectorAll('[class*="conversation-item"]')].filter(
    (el) => !el.className.includes("skeleton") && el.getBoundingClientRect().height > 0
  );
  const target = items.find((el) => el.textContent.includes(NAME));
  if (!target) return { error: "NOT_FOUND", names: items.slice(0, 8).map((el) => el.textContent.trim().slice(0, 16)) };
  target.click();
  return { ok: true };
})()
