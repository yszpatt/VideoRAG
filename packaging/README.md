# packaging —— Windows 桌面包构建

把 videoRAG 打成一个**双击即用**的 Windows 应用。**Docker 分发链路不受影响**：
两条链路共用同一份 `app/` 与 `web/`，差异只在入口、界面载体与本地 ASR 形态。

## 快速开始

```powershell
pwsh -File packaging/build-windows.ps1
```

一条命令走完：构建前端 → 准备依赖 → 获取 ffmpeg → PyInstaller → 冒烟测试 → 打 zip。

常用开关：

| 开关 | 用途 |
|---|---|
| `-SkipWeb` | 前端已构建过，跳过（省时间） |
| `-SkipFfmpeg` | 跳过 ffmpeg 获取（**仅调试打包流程**，正式分发不要用） |
| `-SkipSmoke` | 跳过冒烟测试（不推荐） |
| `-Venv <路径>` | 指定虚拟环境（默认 `./.venv`） |
| `-Version <x.y.z>` | 覆盖 zip 版本号（默认读 `pyproject.toml`） |

## 文件

| 文件 | 作用 |
|---|---|
| `videorag.spec` | PyInstaller 配置（onedir + 双入口）。**原生包与 pywebview 的收集规则都在这里**，出问题先看它 |
| `vendor-ffmpeg.ps1` | 下载 ffmpeg / ffprobe 静态构建到 `vendor/ffmpeg/`（不进仓库） |
| `build-windows.ps1` | 一键构建编排（本地与 CI 共用同一份逻辑） |
| `make_logo.py` | 生成品牌标识与所有图标（SVG / PNG / ICO），几何参数单一来源 |
| `logo/videorag.ico` | exe 图标（多尺寸，由上面脚本生成，已入库） |

## 品牌标识（logo）

标识由 `make_logo.py` **脚本生成**，而不是手工维护图片。原因是同一个标识要出现在 4 个位置、
尺寸与格式各不相同，手工维护必然漂移：

| 产物 | 用途 |
|---|---|
| `web/src/assets/logo.svg` | 侧边栏品牌位（vite 会内联进 JS，无额外请求） |
| `web/public/favicon.svg` + `.ico` | 浏览器标签页（SVG 优先，ICO 回退） |
| `packaging/logo/videorag.ico` | exe 图标（16→256 七档，Windows 按 DPI 自动选） |
| `packaging/logo/videorag-{1024,256}.png` | README / 商店展示 |

设计要点：

- **形制**：圆角方形，圆角比例 31%（与 UI 里 `.brand-mark` 的 `32px / radius 10px` 对齐）
- **配色**：直接复用 UI 主题的 `--grad`（135° 紫 → 粉 → 橙），标识与界面同源
- **图形**：播放三角（视频）+ 两条递减竖条（音频波形 / 被切出的文字片段）。
  只用两个元素是刻意的——侧边栏里它只有 32px，元素一多就糊成一团

改设计只需调整 `make_logo.py` 顶部的几何参数（`TRIANGLE` / `BARS` / `GRAD_STOPS` / `RADIUS`），
然后重跑：

```powershell
.venv\Scripts\python.exe packaging\make_logo.py
```

> **改完要生效，注意三个不同层面**：
>
> 1. **exe 文件图标**（资源管理器 / 快捷方式看到）——构建时嵌入 PE 资源，
>    改了 logo **必须重新打包**；
> 2. **窗口标题栏 / 任务栏图标**——是另一回事，由 `app/desktop.py` 运行时经 pywebview 的
>    `icon=` 设置，图标文件由 spec 放进 `_internal/assets/`。**只改 1 不改 2，窗口图标依旧是
>    解释器的默认图标**（这是实际踩过的坑）；
> 3. **界面里的 logo**——前端资源，重新 `npm run build` 即可。
>
> 另外：SVG 必须是**纯 ASCII**（`make_logo.py` 启动时会自校验）。前端打包器会把小 SVG
> 内联成 `data:` URI，而这种 URI 不允许非 ASCII 字符——中文注释会让 logo 在界面上静默消失。

## 产物

```
dist/
├── videoRAG/                        # 目录形态，可直接运行
│   ├── videorag.exe                 # 窗口形态（双击这个）：原生窗口，无控制台
│   ├── videorag-cli.exe             # 控制台形态：排障 / --self-test / --headless
│   ├── _internal/                   # Python 运行时 + app + web/dist + 全部依赖
│   └── vendor/ffmpeg/               # ffmpeg.exe / ffprobe.exe（启动时注入 PATH）
└── videoRAG-<版本>-windows-x64.zip  # 分发包
```

**为什么是两个 exe**：

- 窗口形态必须 `console=False`（否则双击会先弹一个命令行黑框），而这种进程**没有可用的
  stdout 句柄**——`--self-test` 的结论根本打不出来，CI 也没法读；
- 控制台形态用于 CI 冒烟、命令行排障、`--headless` 当服务器。

两者共用同一个 `Analysis`/`PYZ`，只是 bootloader 的 `console` 开关不同，体积代价可忽略。

## 运行时形态

| | Docker | 桌面包 |
|---|---|---|
| 入口 | `uvicorn app.main:app` | `app/desktop.py` |
| 界面 | 浏览器访问 `:8566` | 原生窗口（Edge WebView2，经 pywebview） |
| 默认端口 | 8566（compose 端口映射） | **8566**（被占自动顺延；避开最易冲突的 8566） |
| ffmpeg | 镜像内 apt 安装 | 随包 `vendor/ffmpeg`，启动注入 PATH |
| 数据目录 | `/data`（compose 注入） | `%LOCALAPPDATA%\videoRAG` |
| 本地 ASR | 侧车容器 HTTP `http://asr:9991` | 进程内 sherpa-onnx（`ASR_LOCAL_BACKEND=inproc`） |

关键：`ASR_LOCAL_BACKEND` 默认 `http`，即**不设置就是 Docker 的老行为**；
桌面入口会把它切成 `inproc`（仅在用户未显式配置、且 `sherpa_onnx` 可导入时）。

### 打包环境下两处必须特殊处理的地方

这两点都是「源码跑得好好的、打包就废」的类型，改代码时务必留意：

1. **yt-dlp 必须同进程调用**（`app/core/fetchers/ytdlp.py::_run_ytdlp_inproc`）。
   抓取层原本用 `sys.executable -m yt_dlp` 起子进程，但打包后 `sys.executable` 是应用 exe，
   且窗口形态没有 stdout 句柄——子进程方案**两头都不成立**。
2. **yt_dlp 必须显式 `collect_all`**。抓取层只用 subprocess 调它，代码里没有任何
   `import yt_dlp`，静态分析发现不了（历史故障：产物里完全没有 yt-dlp）。

## 冒烟测试

```powershell
# 用控制台版（窗口版的 stdout 是 None，看不到结果）
dist\videoRAG\videorag-cli.exe --self-test --data-dir <临时目录>
```

它会检查：`ffmpeg` / `yt-dlp` / `sherpa-onnx` / WebView2 后端 / `/health` /
`/api/videos` / 前端首页 / MCP `tools/list`，任一项失败即非 0 退出。

这几项是有针对性的——它们分别对应历史上真实翻过车的地方（抓取全失败、ASR 不可用、
窗口起不来），比「服务能起来」这种单点检查有价值得多。

## 系统要求

- Windows 10 1803+ / Windows 11
- **WebView2 Runtime**（Win11 与多数 Win10 已自带）
- **.NET 6+ Desktop Runtime** —— 注意是 **Desktop** 而不是 Console 运行时：
  pywebview 的 WinForms 宿主窗口需要桌面框架。开发机通常已装，**干净系统可能没有**

窗口有三级降级链，会自动进行、不会崩：

| 顺序 | 形态 | 依赖 |
|---|---|---|
| 1 | pywebview 原生窗口 | WebView2 + .NET 6+ **Desktop** Runtime |
| 2 | 浏览器 `--app` 窗口（无地址栏 / 无标签页，观感接近原生） | 只需 Edge 或 Chrome（Windows 自带 Edge） |
| 3 | 系统浏览器标签页 | 任意默认浏览器 |

> **踩过的坑**：`_probe_webview()` 原先只 import `webview.platforms.edgechromium`（**渲染层**，
> 不需要 .NET）就报 PASS，但 Windows 上真正的窗口实现是 `webview.platforms.winforms`
> ——它依赖 pythonnet + .NET。于是在缺 .NET 的机器上出现「自检全绿、窗口起不来」。
> 现在探针直接加载 winforms 模块，并把缺失原因写进自检输出。

另外启动时会检测目标端口上是否已有实例：若已有，**只打开它的界面、不再起第二个服务**
（双开会让两个进程共写同一个数据目录，SQLite / LanceDB 有损坏风险）。

## 常见问题

**Q：`uv pip install` 报 “Failed to update Windows PE resources … 拒绝访问”**
uv 在 Windows 上创建 console script 的 trampoline exe 时需要改写 PE 资源，在部分
环境（安全软件、文件过滤驱动、受限沙箱）下会被拒。改用 pip 即可绕开
（pip 用预编译 launcher，直接复制不改写）。`build-windows.ps1` 已内置「uv 失败回落 pip」。

**Q：重装依赖后 `.venv/Lib/site-packages` 整个消失**
`pip install -e` 会先卸载自身的旧版本，个别 Windows 环境（安全软件 / 文件过滤驱动）
下该卸载动作可能异常，导致 site-packages 被清空。恢复：

```powershell
.venv\Scripts\python.exe -m ensurepip --upgrade
.venv\Scripts\python.exe -m pip install -e ".[dev,desktop]"
```

`build-windows.ps1` 会先探测依赖是否齐全，齐全则跳过安装，避免重复触发。

**Q：打包时 `PermissionError: [WinError 5] dist\videoRAG\videorag.exe`**
上一版应用还在运行，文件被占用删不掉。先关掉窗口再打包，
或用 `--distpath dist-next` 打到旁边目录。

**Q：窗口没弹出来，反而开了浏览器**
看日志 `<数据目录>\logs\videorag.log`。常见原因：WebView2 Runtime 缺失、
.NET 6+ 缺失、或 `pywebview`/`pythonnet` 未被正确收集。
用 `videorag-cli.exe --self-test` 可直接看到 `webview backend` 那一项。

**Q：包体积能再小吗？**
已默认排除 faster-whisper 保留档（连带 `ctranslate2` + `av`，省约 **124MB**），
当前解压约 **961MB**。最大的单文件是 lancedb 的 `_lancedb.pyd`（297MB，
上游 Rust 扩展固有体积，配置层面无解）。

其余可裁项及代价（按性价比排序）：

| 策略 | 可省 | 代价 |
|---|---|---|
| 排除视觉旁路（`cv2` + `rapidocr`） | ~128MB | 失去「无语音视频画面文字识别」（MV / PPT 录屏场景） |
| ffmpeg 改首启下载（不随包） | ~196MB | 首次启动需联网；离线环境不可用 |
| 不打包 `ffprobe.exe` | ~98MB | 时长探测降级（桌面默认走进程内 ASR，影响面小） |
| 只保留窗口版 exe | ~26MB | CI 自检与命令行排障失去入口，不推荐 |

组合起来最大可降到约 514MB（解压）/ zip 约 200MB。
完整分析与实测数据见 `docs/plans/2026-09-17-windows-desktop-packaging-design.md` §6。

**Q：要不要用 `--onefile`？**
不要。onefile 每次启动都要把约 1GB 解包到临时目录，冷启动不可接受；onedir 只解压一次。

## 许可

`vendor-ffmpeg.ps1` 下载的 Windows 静态构建通常是 GPL 授权（含 libx264 等）。
该脚本只用于**构建时**获取，仓库不提交这些二进制；分发安装包时请一并提供对应许可文本。
