// 小红书 SSR 数据解包：window.__INITIAL_STATE__ 里全是 Vue ref，需要逐层解包
window.__fbUnwrap = (value) =>
  value && value.__v_isRef ? (value._rawValue !== undefined ? value._rawValue : value._value) : value;
