# 各平台登录 / Cookie 要求，以及扫码登录可行性

> 结论汇总 + 依据。相关实现：`app/core/fetchers/ytdlp.py`（cookie 必需判定与传递）、
> `app/api/cookies.py`（设置页 cookie 配置 API）、`app/core/fetchers/ytdlp.py:30` 的
> `COOKIE_REQUIRED`。
> 面向小红书的分步操作另见 [xiaohongshu-guide.md](xiaohongshu-guide.md)。

## 0. 结论速览

| 平台 | 抓取是否必须登录 | 关键 cookie | 本项目内自建「扫码登录」 |
|---|---|---|---|
| 小红书 `xhs` | **必须** | `web_session`、`a1` | ❌ 不建议（见 §2） |
| 抖音 `douyin` | **必须** | `sessionid`、`sessionid_ss` | ❌ 不建议 |
| B 站 `bilibili` | 可选（高清 / CC 字幕 / 会员 / 私有内容才需要） | `SESSDATA`（写操作另需 `bili_jct`） | ✅ **可行**，协议公开且纯 HTTP |
| YouTube `youtube` | 可选（年龄限制 / 会员 / 私有内容才需要） | `SID`、`SAPISID`、`__Secure-*PSID` | ❌ 无官方途径，OAuth 已失效 |

**现阶段推荐**：统一走设置页的「Cookie / 登录」导入（已实现，覆盖全部平台、零协议风险）；
扫码登录按 §3 的顺序做增量，不要一上来就自建协议。

## 0.5 获取 cookie 的四条路（前两条**不需要装扩展**）

| 方式 | 操作 | 适用形态 | 备注 |
|---|---|---|---|
| ① **F12 复制为 cURL** | 开发者工具 → 网络 → 任一请求右键「复制 → 复制为 cURL (bash)」→ 粘贴到设置页 | 全部（含容器） | **推荐**。已实现自动提取其中的 `Cookie:` 头 |
| ② **复制 Cookie 请求头** | 开发者工具 → 网络 → 请求 → 标头里复制 `Cookie:` 整行 → 粘贴 | 全部 | 同上；两者都**包含 HttpOnly cookie** |
| ③ 从浏览器直接读取 | 设置页选浏览器 → 「从浏览器导入」 | 桌面 / 裸机（容器读不到宿主浏览器） | 走 yt-dlp 的 `extract_cookies_from_browser`，只落该平台的域 |
| ④ 浏览器扩展导出 | 「Get cookies.txt LOCALLY」/「Cookie-Editor」 | 全部 | 最直观，但扩展常被下架 / 不好用，故降为备选 |

为什么①比在控制台敲 `document.cookie` 好用：`document.cookie` **拿不到 HttpOnly**，
而小红书的 `web_session`、B 站的 `SESSDATA` 都是 HttpOnly —— 只复制 `document.cookie`
会得到一个「看起来有 cookie、实际没登录态」的文件。

方式①②粘贴后系统按平台默认域合成 cookie（小红书 `.xiaohongshu.com`、抖音 `.douyin.com`、
B 站 `.bilibili.com`、YouTube `.youtube.com`），接口会回显 `assumed_domain` 告知假定了什么。

方式③的边界：

- **只有服务器本机能看到的浏览器 profile 才能读**。Docker 容器里 `~` 是容器自己的 home，
  默认什么都读不到（设置页会显示「检测不到浏览器」）；要读宿主浏览器得把 profile 目录
  挂进容器（Chrome 系还可能因为缺少 keyring 而解密失败）。
- **Firefox 最稳**（cookie 库是明文 SQLite）；**Chrome / Edge 自 127 起启用 App-Bound 加密**，
  yt-dlp 可能报解密失败，此时请改用①或②。
- 读取会挑出**该平台的 cookie** 落盘，不会把你整库（含其它站点登录态）写进项目。

## 1. 依据：各家到底需要什么

### 1.1 小红书 / 抖音 —— 必需

`app/core/fetchers/ytdlp.py`：

```python
COOKIE_REQUIRED = ("douyin", "xhs")
```

未放 `<cookie_dir>/xhs.txt` 时，提交链接会**立即**抛 `FetchError` 并给出导出提示，不会白跑一遍。
原因：yt-dlp 的 `XiaoHongShu` 提取器是解析页面里的 `window.__INITIAL_STATE__` 拿视频流的
（见其 `_real_extract`），未登录时页面直接是登录引导，取不到 `noteDetailMap` → 报
`No video formats found`。抖音同理（其网页接口另有签名参数，未登录更拿不到）。

### 1.2 B 站 —— 可选，但登录后差别很大

yt-dlp 源码 `yt_dlp/extractor/bilibili.py`：

- 第 57 行：`return bool(self._get_cookies('https://api.bilibili.com').get('SESSDATA'))` —— **`SESSDATA` 就是登录判据**；
- `raise_login_required('This video is for premium members only')` —— 会员 / 课程 / 私有收藏需要登录；
- `need_login_subtitle` → “Subtitles are only available when logged in.” —— **CC 字幕要登录**；
- 画质按 `qn` 逐档降级，未登录时高清档位会被拒。

所以：普通视频不配 cookie 也能下；想要 1080P+ / CC 字幕 / 会员内容就必须配。

### 1.3 YouTube —— 可选，但生态在收紧

依据 [yt-dlp wiki · Extractors](https://github.com/yt-dlp/yt-dlp/wiki/Extractors)：

- cookies **仅在需要账号的内容**上必需：私有播放列表、年龄限制、会员专享；
- YouTube 正在强制 **PO Token**，yt-dlp 无法自行生成，部分格式/功能会缺失（默认客户端仍可用，但可能拿不到全部格式）；
- **OAuth 登录已失效**，官方明确改为「用 cookie」；
- cookie 会被轮换：官方建议在**无痕窗口**登录 → 访问 `youtube.com/robots.txt` → 导出 → **立刻关闭该窗口**，否则导出的 cookie 很快失效；
- 风险提示：官方警告用主账号配合 yt-dlp 有被封风险，建议小号 + 控制请求频率（游客会话约 300 视频/小时）。

## 2. 扫码登录：三条路线的可行性

| 路线 | B 站 | 小红书 / 抖音 | 评价 |
|---|---|---|---|
| (a) 纯 HTTP 自建协议（自己请求二维码接口并轮询） | ✅ 可行 | ❌ 不建议 | B 站协议公开、稳定、无浏览器依赖；小红书/抖音带签名与风控参数，且拿到 cookie 后仍有 X-s/xsec 签名问题 |
| (b) 内嵌真实浏览器（Playwright / WebView2），让官方登录页自己扫码 | ✅ | ✅ | 通用、抗变动（不碰协议）；代价是体积/内存与容器内跑无头浏览器的风控问题 |
| (c) 复用宿主浏览器 cookie（`yt-dlp --cookies-from-browser`） | ✅ | ✅ | yt-dlp 原生支持、零开发量；但容器内拿不到宿主浏览器 profile，只在裸机/桌面形态可用 |

### 2.1 为什么小红书 / 抖音 不适合自建

社区主流做法（如 [MediaCrawler](https://github.com/NanmiCoder/MediaCrawler)）明确选择了
**不逆向**：它用 Playwright/CDP 连接一个真实浏览器，复用登录态后「通过 JS 表达式获取签名参数」，
其命令行就是 `--lt qrcode`：用真实浏览器打开官方登录页、用户扫码、再从浏览器上下文导出登录态。
换句话说——**能扫码，但成本不在二维码，而在之后对抗签名与风控**。自己重写这套协议，
维护成本高（平台一改就废）、且有合规风险，收益与设置页粘贴 cookie 相比很小。

### 2.2 B 站为什么可以做，且值得做

B 站扫码登录是被长期逆向并文档化的公开协议（社区文档
[bilibili-API-collect · 二维码登录](https://lxb007981.github.io/bilibili-API-collect/login/login_action/QR.html)、
[另一镜像](https://goooler.github.io/bilibili-API-collect/docs/login/login_action/QR.html)）：

```
GET https://passport.bilibili.com/x/passport-login/web/qrcode/generate   → {url, qrcode_key}
GET https://passport.bilibili.com/x/passport-login/web/qrcode/poll?qrcode_key=…  → 轮询扫码状态
（旧版为 passport.bilibili.com/qrcode/getLoginUrl + getLoginInfo）
```

扫码成功后响应里带 `SESSDATA` / `bili_jct` / `DedeUserID` 等，可直接写成 Netscape 文件。
前端展示二维码（把 `url` 渲染成二维码图），后端轮询，成功后写 `<cookie_dir>/bilibili.txt`——
**不需要浏览器、Docker 形态也能用**，工作量约几百行。

> 注意：这两个接口是社区文档而非官方承诺，平台随时可能调整；实现时要能优雅降级回「手动粘贴」。

### 2.3 桌面形态的「应用内登录窗口」最划算

Windows 桌面包已经带 pywebview + Edge WebView2（见
[2026-09-17-windows-desktop-packaging-design.md](plans/2026-09-17-windows-desktop-packaging-design.md)）：
开一个窗口指向平台登录页 → 用户扫码 → 关闭窗口时读取 WebView2 的 cookie 并导出 Netscape 文件。
它复用用户自己的浏览器环境（风控风险最低），一套实现通吃小红书 / 抖音 / B 站 / YouTube。

**但它只对桌面形态有意义**：容器里没有用户的浏览器环境，要做就得塞 Playwright + Chromium
（镜像涨几百 MB、内存与风控都要额外处理）。

## 3. 建议的落地顺序

1. ✅ **设置页 cookie 配置**（本次实现，`/api/cookies` + 设置页「Cookie / 登录」页签）：
   粘贴 / 选文件导入、只写不读、保存即生效（无需重启）。覆盖全部平台。
2. **桌面形态的「应用内登录窗口」**（WebView2 扫码）：一次实现吃下四个平台，风险最低；Docker 不做。
3. **B 站纯 HTTP 扫码登录**：若不做第 2 步，B 站可单独做（协议稳定、无浏览器依赖，Docker 也能用）。
4. ❌ 不建议：在容器里为小红书/抖音自建扫码协议，或为此把 Playwright 塞进主镜像。

## 4. 安全与合规

- cookie 等同于账号登录态。本项目的 `/api/cookies` **只回传元信息**（条数/域/关键 cookie 名/过期时间），
  **不回传内容**，文件以 `0600` 落盘、原子写入。
- 但 `/api` 目前**没有鉴权**（既有已知项）：若把 8566 端口暴露到公网，任何人都能写入/清除 cookie。
  建议绑定 `127.0.0.1` 或加一层 API Key（与 `MCP_API_KEY` 同思路）。
- 用登录态做自动化可能与平台条款冲突，风险自担；建议控制频率、优先使用小号。
