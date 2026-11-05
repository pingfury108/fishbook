// IM 页：往输入框写入消息并点发送（回读校验）
(() => {
  const TEXT = __TEXT__;
  const ta = document.querySelector("textarea.ant-input");
  if (!ta) return { error: "NO_INPUT" };

  const setter = Object.getOwnPropertyDescriptor(HTMLTextAreaElement.prototype, "value").set;
  ta.focus();
  setter.call(ta, TEXT);
  ta.dispatchEvent(new Event("input", { bubbles: true }));
  ta.dispatchEvent(new Event("change", { bubbles: true }));
  if (ta.value !== TEXT) return { error: "SET_FAILED" };

  const bottom = document.querySelector('[class*="sendbox-bottom"]');
  const btn =
    bottom && [...bottom.querySelectorAll("*")].find((el) => (el.textContent || "").trim() === "发 送");
  if (!btn) return { error: "NO_SEND_BTN" };
  btn.click();

  return { ok: true, len: TEXT.length };
})()
