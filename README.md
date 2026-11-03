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

# 小红书：搜索（SSR 首屏）/ 笔记详情 / 自检
fishbook --cdp 127.0.0.1:9222 red search "macbook"
fishbook --cdp 127.0.0.1:9222 red item 6ad1ebbd000000001500cab7 --token <xsec_token>
fishbook --cdp 127.0.0.1:9222 red check

# 人眼查看
fishbook -f table xy search "macbook"
```

CDP 地址优先级：`--cdp` > `$FISHBOOK_CDP` > `http://127.0.0.1:9222`。支持三种写法：

| 输入 | 处理 |
|---|---|
| `http://127.0.0.1:9222` | 直接用 |
| `127.0.0.1:9222` | 补 `http://` |
| `ws://…/devtools/page/<id>` | 视为已指定 target，跳过发现 |

`check` 依次检查：端点连通、page target 列表、当前 URL、`unb` / `tracknick` cookie（按 `goofish.com` 查，不按当前地址）、`window.lib.mtop` 就绪、x5sec 风控 cookie、一次最小搜索。

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
| 3 | 登录态失效，需要人工重新登录 |
| 4 | 触发风控 |
| 5 | 连续失败熔断 |

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

## 小红书（red）

不用 API，直接读页面自身 SSR 下来的 `window.__INITIAL_STATE__`，请求量等于普通浏览一次页面。

为什么不用 API：`/api/sns/web/v1/search/notes`、`/api/sns/web/v1/feed` 需要 `X-s` / `X-t` /
`X-S-Common` 三件套，其中 `X-s`/`X-t` 可由页面里的 `window._webmsxyw(api, payload)` 现算，
但实测（已签名、已带上真实 `X-S-Common`）仍返回：

```
{"code":300011,"success":false,"msg":"当前账号存在异常，请切换账号后重试"}
```

且服务端会把 `web_session` 降级（cookie 里多一个 `last_web_session`），页面变成「登录后查看搜索结果」。

| 子命令 | 说明 |
|---|---|
| `red search <kw> [--limit 20]` | 只能拿到 SSR 首屏（实测 ~22 条），翻页需 API，当前环境不可用 |
| `red item <note_id> [--token <xsec_token>]` | `xsec_token` 从 `red search` 结果里的 `xsec_token` 字段拿 |
| `red check [--keyword]` | 检查 `web_session` 是否还在、是否被降级（`last_web_session`）、最小搜索 |

登录态判定：有 `web_session` 且**没有** `last_web_session`；降级/未登录都返回退出码 3。

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
    web.py            SSR 导航 / 登录态判定 / __INITIAL_STATE__ 解包
    search.py         搜索：读首屏 SSR
    item.py           笔记详情：读详情页 SSR
    unwrap.js         解 Vue ref
    search_state.js   搜索页取数 JS
    note_state.js     详情页取数 JS
  util.py             日志 / 抖动 / 数字归一
```

## 加站点

仿 `sites/xy/`（API 驱动）或 `sites/red/`（SSR 驱动）新建目录，在 `cli.py` 的 `DOMAINS` 里登记域名即可。
