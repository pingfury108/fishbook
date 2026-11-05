// IM 页：只填入输入框并回读校验，不点发送（用于链路测试）
(() => {
  const TEXT = __TEXT__;
  const ta = document.querySelector("textarea.ant-input");
  if (!ta) return { error: "NO_INPUT" };

  const setter = Object.getOwnPropertyDescriptor(HTMLTextAreaElement.prototype, "value").set;
  ta.focus();
  setter.call(ta, TEXT);
  ta.dispatchEvent(new Event("input", { bubbles: true }));
  ta.dispatchEvent(new Event("change", { bubbles: true }));
  const value = ta.value;
  const bottom = document.querySelector('[class*="sendbox-bottom"]');
  const sendBtnReady = !!(
    bottom && [...bottom.querySelectorAll("*")].find((el) => (el.textContent || "").trim() === "发 送")
  );

  // 清空还原
  setter.call(ta, "");
  ta.dispatchEvent(new Event("input", { bubbles: true }));

  return { filled: value === TEXT, sendBtnReady: sendBtnReady, cleared: ta.value === "" };
})()
