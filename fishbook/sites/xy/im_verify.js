// IM 页：发送后校验（输入框已清空 + 消息流里出现刚发的那条右侧消息）
(() => {
  const TEXT = __TEXT__;
  const ta = document.querySelector("textarea.ant-input");
  const sc = document.querySelector('[class*="scroll-container"]');
  const cleared = !ta || ta.value === "";
  const sent =
    sc &&
    [...sc.querySelectorAll('[class*="message-text"]')]
      .filter((el) => el.className.includes("right"))
      .some((el) => (el.textContent || "").trim() === TEXT);
  return { cleared: cleared, appears: !!sent };
})()
