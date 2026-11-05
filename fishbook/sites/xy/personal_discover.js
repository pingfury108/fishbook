// 「我发布的」在售商品解析：a[href*="item?id="] → {item_id, title, price}
(() => {
  const seen = {};
  const clean = (el) => (el ? (el.textContent || "").replace(/\s+/g, " ").trim() : "");

  for (const a of document.querySelectorAll('a[href*="item?id="]')) {
    const href = a.getAttribute("href") || "";
    const id = (href.match(/[?&]id=(\d+)/) || [])[1];
    if (!id || seen[id]) continue;

    let root = a;
    for (let i = 0; i < 5 && root && root !== document.body; i++) {
      const t = clean(root);
      if (/[¥￥]/.test(t) && t.length > 10) break;
      root = root.parentElement;
    }
    const scope = root || a;

    // 价格：price-wrap 容器里除「¥」符号外的数字（卡片文本会把「¥40」「9人想要」粘一起）
    const priceWrap = scope.querySelector('[class*="price-wrap"]');
    let price = "";
    if (priceWrap) {
      // 价格被拆成「¥」+ 整数 + 小数点 + 小数 多个叶子节点，取整块再去掉符号
      const raw = clean(priceWrap).replace(/[¥￥\s]/g, "");
      const matched = raw.match(/^[\d,]+(\.\d{1,2})?/);
      price = matched ? matched[0].replace(/,/g, "") : "";
    }
    if (!price) {
      price = (clean(scope).match(/[¥￥]\s*([\d,]+(?:\.\d{1,2})?)/) || [])[1] || "";
    }

    // 标题：卡片文本里第一个价格符号之前的部分，去掉尾部营销标签
    let title = clean(scope).split(/[¥￥]/)[0].trim();
    title = title
      .replace(/(包邮|热销第\d+名|行情价[\d.]+元|\d+人想要)$/g, "")
      .replace(/\s+/g, " ")
      .trim()
      .slice(0, 60);

    seen[id] = { item_id: id, title: title || "（未取到标题）", price: price };
  }
  return { url: location.href, items: Object.values(seen) };
})()
