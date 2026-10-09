# 小红书（xiaohongshu）视频下载与转笔记 · 使用说明

> 适用版本：本仓库当前 `main`。涉及 code：`app/core/fetchers/ytdlp.py`（抓取 + cookie）、
> `app/jobs/pipeline.py`（摄入链路）。抖音（`douyin.txt`）同理，把下文的 `xhs` 换成 `douyin` 即可。

## 0. 一句话

**导出登录 cookie → 存成 `<数据目录>/cookies/xhs.txt` → 在界面「导入视频」粘贴
带 `xsec_token` 的完整链接**。之后：抓元数据 → 下载音频 → 本地 SenseVoice 转写 →
LLM 生成笔记 → 切片入库，可检索 / 可提问。

---

## 1. 为什么必须给 cookie

小红书未登录访问笔记详情会被引导到登录页；`yt-dlp` 的 `XiaoHongShu` 提取器是解析页面里的
`window.__INITIAL_STATE__` 拿视频流的，登录态不足就只剩一句
`ERROR: [XiaoHongShu] ...: No video formats found!`。

因此本项目把小红书 / 抖音列为**必需 cookie 的平台**（`COOKIE_REQUIRED`）：没有 cookie
文件时，**提交链接会立刻返回明确错误**，不会白跑一遍下载再失败。

---

## 2. 拿到 cookie（一次性，约 1 分钟）

**不需要装浏览器扩展**，三条路任选：

### 路 1（推荐）：F12 → 复制为 cURL

1. 在浏览器登录 <https://www.xiaohongshu.com>；
2. 按 <kbd>F12</kbd> → 「**网络 / Network**」标签；
3. 刷新页面或随便点一下，让请求列表出现发往 xiaohongshu.com 的请求；
4. 右键任意一条 → 「**复制 → 复制为 cURL (bash)**」；
5. 到设置页「Cookie / 登录 → 小红书」粘贴 → **保存**（格式自动识别，会回显假定的域）。

### 路 2：复制请求头里的 `Cookie:` 一行

同上打开 F12 → 网络 → 点开任一请求 → 「标头 / Headers → 请求标头」→ 找到 `Cookie:` 那一行
整行复制 → 粘贴 → 保存。

> 这两条路都带 **HttpOnly** cookie（`web_session` 就是 HttpOnly）。**不要**在控制台敲
> `document.cookie` 复制——它拿不到 HttpOnly，会得到一个「看着有 cookie、实际没登录」的文件。

### 路 3（桌面 / 裸机）：从本机浏览器直接读取

设置页「Cookie / 登录 → 小红书」下方选浏览器 → 「从浏览器导入」。走的是 yt-dlp 的
`--cookies-from-browser` 实现，读取本机已登录的 profile 并只挑出小红书的 cookie。

- 容器形态读不到宿主浏览器（设置页会显示「检测不到浏览器」）；
- Firefox 最稳；Chrome / Edge 自 127 起有 App-Bound 加密，可能解密失败 → 改用路 1。

### 路 4（备选）：浏览器扩展导出

装「Get cookies.txt LOCALLY」导出 Netscape 文本（或「Cookie-Editor」导出 JSON），
粘贴或选文件导入。扩展常被应用商店下架、也容易被站点风控盯上，故排在最后。

> cookie 等同账号登录态：**别提交进 git、别贴到 issue 里**。本仓库的 `.env`、`.data/`、
> `downloads/` 都已在 `.gitignore` 中。

---

## 3. 放到哪里

程序读 `<COOKIE_DIR>/<platform>.txt`，`COOKIE_DIR` 默认是 `<数据目录>/cookies`
（可用环境变量 `COOKIE_DIR` 覆盖）。有两种放法，**推荐第一种**。

### 方式 A（推荐）：设置页导入

「设置 → **Cookie / 登录**」页签里找到「小红书」，把第 2 步拿到的东西粘贴进输入框
（F12 的 cURL、`Cookie:` 请求头、Netscape 文本、扩展 JSON **都认**；也可点「选择文件」；
桌面 / 裸机还可以直接「从浏览器导入」），点**保存**。页面会显示条数、域、关键 cookie
（`web_session` / `a1`）是否命中、是否过期；文件以 `0600` 落在 `<COOKIE_DIR>/xhs.txt`。
接口只回传元信息、不回显内容，保存后输入框会被清空。

### 方式 B：手工放文件

| 形态 | 数据目录 | cookie 最终路径 |
|---|---|---|
| Docker compose（本仓库默认） | 宿主 `./.data` ↔ 容器 `/data` | 宿主 `./.data/cookies/xhs.txt` |
| 裸机 / 源码运行 | `$DATA_DIR` | `$DATA_DIR/cookies/xhs.txt` |
| Windows 桌面包 | `%LOCALAPPDATA%\videoRAG` | `%LOCALAPPDATA%\videoRAG\cookies\xhs.txt` |

Docker 形态（本机）：

```bash
cd /home/yszpat/videoRAG
mkdir -p .data/cookies
cp ~/Downloads/xhs.txt .data/cookies/xhs.txt
chmod 600 .data/cookies/xhs.txt
# 容器以非 root 的 app 用户读这份文件，属主要与 VIDEORAG_UID/GID 对得上（默认 1000:1000）：
ls -ln .data/cookies/xhs.txt
```

两种方式都不需要重启容器：cookie 是每次提交任务时现读的，替换后下一次导入即生效。

---

## 4. 提交什么链接（最容易踩的一步）

| 链接形态 | 能否用 | 说明 |
|---|---|---|
| `https://www.xiaohongshu.com/explore/<24位十六进制id>?xsec_token=...&xsec_source=pc_feed` | ✅ 推荐 | **务必保留 `xsec_token`**：小红书对笔记详情做签名校验，从浏览器地址栏复制的完整链接才带。去掉或过期常报 `No video formats found` |
| `https://www.xiaohongshu.com/discovery/item/<id>?xsec_token=...` | ✅ | `yt-dlp` 提取器同样匹配（其官方测试用例即此形态） |
| `https://xhslink.com/xxxxxx`（App 分享短链） | ⚠️ 可能失败 | `yt-dlp` 的 `XiaoHongShu` 提取器只匹配 `www.xiaohongshu.com` 的 `explore` / `discovery/item`，短链会落到通用提取器；而本项目把 `xhslink.com` 识别为小红书，因此仍会要求 cookie。**建议先在浏览器打开短链，复制展开后的地址** |
| 图文笔记（没有视频） | ❌ | 提取器只取视频流，纯图文没有媒体可下 |
| 直播 / 合集 | ❌ | 未支持 |

提交方式任选：

- Web 界面右上角「**导入视频**」，粘贴链接；
- `POST /api/videos`，body `{"url": "..."}`；
- MCP 工具 `submit_video`（给外部 agent 用）。

---

## 5. 提交之后会发生什么

```
queued → fetching → transcribing → noting → embedding → done
```

1. **元数据**：标题 / 作者 / 简介 / 封面 / 标签 / 时长 / 点赞数
   （`--dump-json --write-comments`）；失败只记日志、不 fail 视频（`meta_source=none`，
   卡片回退显示链接）。
2. **媒体**：字幕（小红书一般没有字幕轨）→ 音频 `-f bestaudio/best -x` 转 **mp3** → 视频兜底。
3. **转写**：本地 SenseVoice（Docker 走侧车 `http://asr:9991`）。
4. **笔记**：LLM 生成（默认 DeepSeek；简介与热评会注入笔记上下文）。
5. **入库**：切片 + 向量（fastembed `bge-small-zh-v1.5`），此后「检索 / 提问」能命中，
   引用带时间戳可跳回原视频。

想留存下载到的 mp3：设置 `MEDIA_SAVE_DIR=/media`（compose 已把它映射到宿主
`${MEDIA_SAVE_DIR_HOST:-./downloads}`），文件按标题命名并以 `video_id` 保底。

---

## 6. 怎么确认 cookie / 链接真的有效

不用等整条流水线，直接在容器里跑一次只抓元数据的命令（最快）：

```bash
cd /home/yszpat/videoRAG
docker compose exec -T videorag python -m yt_dlp \
  --cookies=/data/cookies/xhs.txt \
  --dump-json --skip-download --no-playlist \
  "<粘贴带 xsec_token 的完整链接>" | head -c 400
```

- 打出 JSON（含 `"title"` / `"duration"` / `"formats"`）→ cookie 与链接都没问题；
- 报 `No video formats found` → cookie 过期 / 链接缺 `xsec_token` / 该笔记没有视频；
- 报 `Unable to download webpage` / 403 → 通常是 cookie 失效或被风控。

看服务日志：

```bash
docker compose logs -f videorag
```

---

## 7. 常见问题

| 现象 | 原因 | 处理 |
|---|---|---|
| 提交即报「xhs 视频需要登录 cookie」 | 没放 `xhs.txt` | 第 2、3 步导出并放置 |
| 卡片 error：`No video formats found` | cookie 过期、链接缺 `xsec_token`、或图文笔记 | 重新导出 cookie；改用浏览器地址栏完整链接；确认是视频笔记 |
| 卡片只显示链接、没有标题/封面/热评 | 元数据采集失败（`meta_source=none`），已被隔离 | 多为 cookie 失效；重新导出后**重新处理该视频** |
| `403` / 需要验证 | 小红书风控 | 降低频率、重新登录导出 cookie；避免短时间批量导入 |
| 昨天能下、今天不能 | 小红书接口/风控变动频繁 | 先升级 `yt-dlp`：`docker compose build --no-cache videorag && docker compose up -d videorag` |
| 想核对容器里到底有没有 cookie 文件 | — | `docker compose exec -T videorag ls -l /data/cookies` |

> **历史坑（已修）**：元数据采集与视觉旁路用的 cookie 参数曾把路径拼成 `<dir>/xhs`（漏了
> `.txt`）。`yt-dlp` 对不存在的 `--cookies` 文件是**静默容忍**的（不报错、当没传），所以那两条
> 路径其实一直在无 cookie 状态下跑——症状就是卡片只显示链接、笔记上下文里没有简介与热评。
> 主摄入链路不受影响（它自己拼的路径是对的）。修复见 `cookie_args_for()` 与其回归用例。

---

## 8. 相关代码与测试

| 位置 | 作用 |
|---|---|
| `app/core/fetchers/ytdlp.py` → `detect_platform()` | `xiaohongshu.com` / `xhslink.com` → `xhs` |
| 同文件 `COOKIE_REQUIRED` / `cookie_args_for()` / `YtdlpFetcher._cookie_path_for()` | cookie 必需判定与 `--cookies` 参数拼装 |
| 同文件 `fetch_metadata()` / `fetch_video()` | 元数据采集、视觉旁路按需下视频（都用 `cookie_args_for`） |
| `app/jobs/pipeline.py` → `process_video()` | 摄入链路编排 |
| `tests/test_fetchers.py`、`tests/test_metadata.py` | 平台识别、cookie 传递、免 cookie 报错提示 |
