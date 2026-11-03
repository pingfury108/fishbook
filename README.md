# fishbook

> 把浏览器当成「带登录态的 JS 运行时」，在页面里调站点自己的 RPC，把结构化数据打到 stdout。

- 站点用子命令区分：`fishbook xy ...`（闲鱼），`red`（小红书）预留
- 只连一个 CDP 地址，不启动浏览器、不管 profile、不装扩展、不存数据
- 只读：只做 search / item

## 安装

```bash
uv sync          # 自动装 Python 3.12 + 依赖
```

## 前置条件（工具不负责）

Chrome 136+ 无法用调试端口连默认 profile，被连的浏览器必须是独立 `--user-data-dir` 起的一次性实例，并在里面登录过闲鱼：

```bash
"/Applications/Google Chrome.app/Contents/MacOS/Google Chrome" \
  --remote-debugging-port=9222 \
  --user-data-dir=/tmp/chrome-fishbook
```

## 用法

```bash
# 自检（排障第一步永远是它）
fishbook --cdp 127.0.0.1:9222 check

# 关键词搜索
fishbook --cdp 127.0.0.1:9222 xy search "macbook" --limit 30

# 带筛选：价格 + 城市
fishbook --cdp 127.0.0.1:9222 xy search "相机" --min-price 1000 --max-price 3000 --province 浙江 --city 杭州

# 商品详情
fishbook --cdp 127.0.0.1:9222 xy item 1040754408976

# 小红书：搜索 / 笔记详情（UI 自动化，不调接口）
fishbook --cdp 127.0.0.1:9222 red search "macbook" --limit 20
fishbook --cdp 127.0.0.1:9222 red note "https://www.xiaohongshu.com/explore/6ad1ebbd000000001500cab7?xsec_token=ABUp..."

# 站点级自检细节
fishbook --cdp 127.0.0.1:9222 check --site xy
fishbook --cdp 127.0.0.1:9222 check --site red --keyword macbook

# 人眼查看
fishbook -f table xy search "macbook"
```

CDP 地址优先级：`--cdp` > `$FISHBOOK_CDP` > `http://127.0.0.1:9222`。支持三种写法：

| 输入 | 处理 |
|---|---|
| `http://127.0.0.1:9222` | 直接用 |
| `127.0.0.1:9222` | 补 `http://` |
| `ws://…/devtools/page/<id>` | 视为已指定 target，跳过发现 |

`check` 的通用部分是端点连通 / page target 列表 / 当前 URL / cookie；加 `--site` 再叠加站点细节：

| 命令 | 追加检查 |
|---|---|
| `check --site xy` | `unb` / `tracknick` cookie、`window.lib.mtop` 就绪、x5sec 痕迹（`--keyword` 时加一次最小搜索） |
| `check --site red` | `web_session` cookie、登录态是否被降级、`#search-input` 是否存在（`--keyword` 时加一次最小搜索） |

target 发现：优先选 url 命中 `goofish.com` 的 page target；若只有一个 page target（比如 `chrome://newtab`）就回退到它并由工具导航过去；多个都不匹配则报错并列出。

## 输出

```
stdout  →  {"ok": true, "data": [...]}
        →  {"ok": false, "error": {"code": "...", "message": "..."}}
stderr  →  进度 / 重试 / 风控告警
```

| 退出码 | 含义 |
|---|---|
| 0 | 成功 |
| 1 | 参数错误 / MTOP 业务错误 |
| 2 | 连不上 CDP 端点 / 找不到匹配的 page target / 页面未就绪 |
| 3 | 未登录 / 登录态失效 / 目标不存在 |
| 4 | 触发风控 |
| 5 | 页面结构不符（选择器失效、输入框不唯一等） |
| 6 | 连续失败熔断 |

## 限速

串行 + 请求间 `sleep(1~3s)` 抖动；连续 2 次异常暂停 10 分钟，第 3 次直接退出；单次 `--limit` 上限 60（2 页）；同一次运行内按 `item_id` 去重。

## 字段（按实测响应校正）

列表项（`data.resultList[]`，真实路径是 `entry.data.item.main`）：

| 字段 | 来源 |
|---|---|
| `item_id` | `main.exContent.itemId`（兜底 `clickParam.args.item_id`） |
| `title` | `main.exContent.title`（闲鱼列表里就是整段描述） |
| `price` | `clickParam.args.price`（兜底 `exContent.detailParams.soldPrice`） |
| `original_price` | `main.exContent.oriPrice` |
| `city` | `main.exContent.area`（省份级，接口没有 `p_city`） |
| `want_cnt` | `fishTags.r3` 里的 `N人想要`（`args.wantNum` 常年是 0） |
| `seller_nick` | `main.exContent.userNickName` |
| `pic` | `main.exContent.picUrl` |
| `tags` | `fishTags.r2/r3/r4[].tagList[].data.content`（`r1` 是 `freeShippingIcon` 之类图标名，丢弃） |

翻页终止用 `resultInfo.hasNextPage`，而不是只看「本页不足 30 条」。

详情（`mtop.taobao.idle.pc.detail`，真实响应在 `data.itemDO` / `data.sellerDO`）：

| 字段 | 来源 |
|---|---|
| `item_id` / `title` / `desc` | `itemDO.itemId` / `.title` / `.desc` |
| `price` | `itemDO.soldPrice`（兜底 `defaultPrice`，注意它可能是字符串 `"False"`） |
| `original_price` | `itemDO.originalPrice`（`"0"` 视为没有 → `null`） |
| `want_cnt` / `collect_cnt` / `browse_cnt` | `itemDO.wantCnt` / `.collectCnt` / `.browseCnt` |
| `status` | `itemDO.itemStatusStr` |
| `images` | `itemDO.imageInfos[].url` |
| `labels` | `itemDO.itemLabelExtList[].text`（分类 / 品牌 / 内存…） |
| `seller` | `sellerDO`: `nick` `sellerId` `publishCity` `xianyuSummary` `replyRatio24h` `replyInterval` |

## 小红书（red）：没有 SDK 可借，只能点页面

小红书 web API（`edith.xiaohongshu.com/api/sns/web/...`）要 `X-s` / `X-t` / `X-S-Common` 三个头。
`window._webmsxyw` 只产生前两个；`X-S-Common` 与浏览器指纹绑定，手工 `fetch()` 会被
**461 / 300015「浏览器运行环境异常」** 拒绝。本仓库实测（现算 `X-s`/`X-t` + 实时抓到的真实
`X-S-Common`，签名本身有效，`user/me` 能返回 `code 0`）后仍然吃：

```
{"code":300011,"success":false,"msg":"当前账号存在异常，请切换账号后重试"}
```

并且服务端会把 `web_session` 降级成 `last_web_session`，页面变成「登录后查看搜索结果」。
**结论：闲鱼的 mtop 模式无法平移到小红书，不自己算签名，也不走 `evaluate(fetch)`。**

所以走页面自身的搜索链路，请求量等于人工浏览：

| 步骤 | 做法 |
|---|---|
| 1 | `goto https://www.xiaohongshu.com/explore`，等 `#search-input` **唯一且可见** |
| 2 | 用 `HTMLInputElement.prototype.value` 原生 setter 赋值，派发 `input` + `change`，**回读校验** |
| 3 | 找唯一的 `.search-icon` 并 `click()` |
| 4 | 等 `location.pathname === '/search_result'`、`?keyword=` 等于关键词，且出现 `section.note-item` 或 `.search-empty-wrapper` |
| 5 | 滚动收割：`scrollTo(0, scrollHeight)` → 收集卡片 → 按 `note_id` 去重，直到够 `--limit` / `--max-scrolls` 用完 / 连续 2 轮无新增 |

可见性判定三层：`getBoundingClientRect` 非零、逐级 `display/visibility/opacity`、排除 `[hidden] [aria-hidden] [inert]` 祖先。

单卡字段：

| 字段 | 来源 |
|---|---|
| `note_id` | 卡片内 `a[href*="/search_result/"]` 路径正则 `/(?:search_result\|explore\|note)/([0-9a-f]{24})` |
| `note_url` | **完整 URL（含 `xsec_token`），必须原样保留** |
| `title` / `author` / `likes` | 卡片内文本节点，`likes` 用 `count.py` 解析 |
| `type` | 卡片内有 `.play-icon` 等则 `video` |

> ⚠️ `xsec_token` 是硬约束：详情页**不能只靠 `note_id` 打开**。所以 `search` 输出必须带完整
> `note_url`，`red note` 也优先接受完整 URL。这是和闲鱼最大的输出差异——闲鱼 `item_id` 自足。

`red note` 是 DOM 提取，**作用域锁死在 `#noteContainer`**，否则会串到右侧推荐流：

| 字段 | 选择器 |
|---|---|
| `title` / `desc` | `#detail-title` / `#detail-desc` |
| `author` | `.username` |
| `likes` / `collects` / `comments` | `.interact-container` 内 `.like-wrapper .count` / `.collect-wrapper .count` / `.chat-wrapper .count` |
| `tags` | `#detail-desc a.tag`（去掉开头 `#`） |
| `images` | `.media-container img` / `.swiper-slide img` |

计数解析：`9776` / `1.2万` / `3亿` / `9.8K` / `1.5M` → 整数；**解析不了返回 `null`，不给 0**。

### 风控

| 类型 | 判据 |
|---|---|
| 限速页 | URL 命中 `/website-login/error`、`error_code=300017`、`error_code=300031` |
| 验证码 | URL 命中 `/website-login/captcha` |
| 文案 | body 含 `安全限制` / `访问链接异常` / `请求太频繁` / `访问频次异常` |
| 登录墙 | body 含 `登录后查看` / `请登录`，或 cookie 出现 `last_web_session` |

处理：**冷却 8~18s 随机，重载一次，再挡就退出码 4**。写成一个 `if`，绝不写成 loop。

节奏：详情页 settle 2~5s 随机（闲鱼 1~3s）；搜索页只在滚动间停 1~2s。

## 排障

| 症状 | 原因 / 处理 |
|---|---|
| `WS 连接失败 … did not receive a valid HTTP response` | `https_proxy` 之类的环境变量把 CDP 的 WS 也代理了；代码里已固定 `proxy=None`，若你自己封装注意同样要关 |
| `没有匹配 goofish.com 的 page target` | 浏览器开着多个标签且都不在闲鱼；用 `--cdp ws://…/devtools/page/<id>` 指定，或先手动打开一个闲鱼标签 |
| `MTOP 业务错误: TIMEOUT::接口超时` | 通常是 x5sec 风控惩罚：页面会跳到 `…/_____tmd_____/punish?x5secdata=…`。
解决方案：在那台浏览器里打开商品页人工过验证，再重试 |
| `check` 里 `x5sec 痕迹` 显示存在 | 只是惩罚期留的 httpOnly cookie（h5api 域），**不**代表当前一定被拦，仅作限速提醒 |
| `登录态失效` | `unb` / `tracknick` 过期，在那台浏览器上重新登录 |
| `页面未就绪` | 8~12s 内 mtop SDK 没出现，通常是网络慢或未真正加载完；重跑一次 |
| 小红书 `error: AUTH_REQUIRED` 且提示「登录态已被风控降级」 | `web_session` 被挪到 `last_web_session`，去浏览器重新登录 |
| 小红书 `STRUCTURE`（退出码 5） | 页面改版导致选择器失效；先 `check --site red` 看 `#search-input` 是否还在 |
| 小红书 `TIMEOUT::接口超时` / `300011` | 别调接口。这是账号级风控，等冷却并重新登录，只走 `red search` / `red note` |

## 目录

```
fishbook/
  cdp.py              endpoint 归一化 + target 发现 + WS 收发 + evaluate
  cli.py              check | xy search | xy item，输出与退出码
  errors.py           错误类型与退出码
  sites/xy/
    mtop.py           模板渲染 / 错误码分类 / token 刷新 / 风控检测
    search.py         搜索：筛选、翻页、去重、限速、字段映射
    item.py           详情：字段映射
    mtop_search.js    页面内 JS，参数用 __KEY__ 占位
    mtop_detail.js
  sites/red/
    web.py            导航 + 就绪轮询 + 登录态判定 + settle
    search.js         页面内：输入 + 点击 + 滚动 + 收割
    note.js           页面内：作用域提取 #noteContainer
    count.py          "1.2万" → 12000
    risk.py           风控识别 + 单次冷却重试
    search.py         搜索编排
    note.py           详情编排
  util.py             日志 / 抖动 / 数字归一
```

## 加站点

仿 `sites/xy/`（借站点 SDK）或 `sites/red/`（点页面 + DOM）新建目录，在 `cli.py` 的 `DOMAINS` 里登记域名即可。
