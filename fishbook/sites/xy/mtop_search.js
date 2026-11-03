// 闲鱼搜索：在页面主世界调用 mtop，签名 / token 全由页面 SDK 负责。
// 占位符：__PAGE__ __KEYWORD__ __PROP_VALUE_STR__ __EXTRA_FILTER_VALUE__ __FROM_FILTER__
(async () => {
  const parse = (value) => {
    if (typeof value !== 'string') return value;
    try { return JSON.parse(value); } catch (_) { return value; }
  };

  const mtop = window.lib && window.lib.mtop;
  if (!mtop || typeof mtop.request !== 'function') {
    return { error: 'MTOP_NOT_READY', ret: null, data: null };
  }

  const request = {
    type: 'POST',
    v: '1.0',
    dataType: 'json',
    needLogin: false,
    needLoginPC: false,
    sessionOption: 'AutoLoginOnly',
    ecode: 0,
    api: 'mtop.taobao.idlemtopsearch.pc.search',
    data: {
      pageNumber: __PAGE__,
      keyword: __KEYWORD__,
      rowsPerPage: 30,
      sortValue: '',
      sortField: '',
      customDistance: '',
      gps: '',
      customGps: '',
      searchReqFromPage: 'pcSearch',
      userPositionJson: '{}',
      propValueStr: __PROP_VALUE_STR__,
      extraFilterValue: __EXTRA_FILTER_VALUE__,
      fromFilter: __FROM_FILTER__
    }
  };

  try {
    const res = await mtop.request(request);
    return { error: null, ret: (res && res.ret) || null, data: parse(res && res.data) };
  } catch (err) {
    return {
      error: String((err && err.message) || err),
      ret: (err && err.ret) || null,
      data: parse(err && err.data)
    };
  }
})()
