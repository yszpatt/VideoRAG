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
| `videorag.spec` | PyInstaller 配置（onedir，单一窗口入口）。**原生包与 WebView2Loader 的收集规则都在这里**，出问题先看它 |
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
> 2. **窗口标题栏 / 任务栏图标**——是另一回事，由 `app/desktop.py` 运行时交给
>    `app/webview2_host.py`（Win32 `LoadImageW` + 窗口类 `hIcon`）设置，图标文件由
>    spec 放进 `_internal/assets/`。**只改 1 不改 2，窗口图标依旧是系统的默认图标**
>    （这是实际踩过的坑）；
> 3. **界面里的 logo**——前端资源，重新 `npm run build` 即可。
>
> 另外：SVG 必须是**纯 ASCII**（`make_logo.py` 启动时会自校验）。前端打包器会把小 SVG
> 内联成 `data:` URI，而这种 URI 不允许非 ASCII 字符——中文注释会让 logo 在界面上静默消失。

## 产物

```
dist/
├── videoRAG/                        # 目录形态，可直接运行
│   ├── videorag.exe                 # 唯一入口（双击这个）：原生窗口，无控制台
│   ├── _internal/                   # Python 运行时 + app + web/dist + 全部依赖
│   │   └── vendor/webview2/         # WebView2Loader.dll（定位系统 WebView2 Runtime）
│   └── vendor/ffmpeg/               # ffmpeg.exe（启动时注入 PATH；不含 ffprobe）
└── videoRAG-<版本>-windows-x64.zip  # 分发包
```

**为什么发布包只有一个 exe**：桌面版的形态就是「一个应用程序窗口」。用户双击
`videorag.exe` 就得到窗口，不需要命令行、也不经过浏览器。

代价是窗口形态必须 `console=False`（否则双击会先弹一个黑框），而这种进程**没有可用的
stdout 句柄**——`--self-test` 的结论打不出来。解决办法不是把控制台入口塞进发布包，
而是**按需临时构建**：`build-windows.ps1` 在冒烟步骤用 `VIDEORAG_BUILD_CLI=1` 让
同一份 spec 产出 `videorag-cli.exe`，跑完自检立即删除，zip 里始终只有一个 exe。
spec 是 Python 脚本，因此「一份 spec、两种入口」不需要维护两份配置。

## 运行时形态

| | Docker | 桌面包 |
|---|---|---|
| 入口 | `uvicorn app.main:app` | `app/desktop.py` |
| 界面 | 浏览器访问 `:8566` | 原生窗口（直连 WebView2 Win32/COM，**不经 .NET**） |
| 默认端口 | 8566（compose 端口映射） | **8566**（被占自动顺延；避开最易冲突的 8080） |
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
# 先单独构建控制台入口（发布包里不含它，仅为冒烟/排障）
$env:VIDEORAG_BUILD_CLI = '1'
.venv\Scripts\python.exe -m PyInstaller packaging/videorag.spec --noconfirm --distpath dist --workpath build-cli
dist\videoRAG-cli\videorag-cli.exe --self-test --data-dir <临时目录>
```

它会检查：`ffmpeg` / `yt-dlp` / `sherpa-onnx` / WebView2 后端 / `/health` /
`/api/videos` / 前端首页 / MCP `tools/list`，任一项失败即非 0 退出。

这几项是有针对性的——它们分别对应历史上真实翻过车的地方（抓取全失败、ASR 不可用、
窗口起不来），比「服务能起来」这种单点检查有价值得多。

## 系统要求

- Windows 10 1803+ / Windows 11
- **Microsoft Edge WebView2 Runtime**（Win11 与多数 Win10 已自带；缺失时从
  [官方页面](https://developer.microsoft.com/microsoft-edge/webview2/) 安装）
- **不需要 .NET**。这一点是本包与早期版本的关键差别，见下。

窗口只有一种形态：WebView2 原生窗口（`app/webview2_host.py`，纯 ctypes + COM）。
**没有浏览器降级**——窗口起不来时直接弹窗说明原因并写日志，不会偷偷开浏览器。

> **踩过的坑（两代故障，都是窗口起不来）**
>
> 1. 早期用 pywebview，Windows 上走 `webview.platforms.winforms` → `pythonnet` →
>    .NET Framework。`clr_loader` 默认选 netfx，用 `Assembly.LoadFrom` 加载
>    `Python.Runtime.dll`；当该路径被 .NET 判定为「网络位置」时会触发 CAS 策略拒绝
>    （`0x80131515` / `loadFromRemoteSources`），报
>    `Failed to resolve Python.Runtime.Loader.Initialize`。**装任何 .NET Desktop
>    Runtime 都无济于事**，因为失败发生在加载策略而不是运行时缺失。
> 2. 想改走 CoreCLR 也不行：`System.Windows.Forms.ContextMenu` 在 .NET 5+ 已被移除，
>    WinForms 版 WebView2 控件在 CoreCLR 下无法实例化（`TypeLoadException`）。
>
> 结论：WinForms 宿主只能跑在 .NET Framework 上，而 .NET Framework 的加载策略不可控。
> 现在的做法是**完全绕开 CLR**，直接调 WebView2 的 COM 接口（`ICoreWebView2Environment`
> → `ICoreWebView2Controller` → `ICoreWebView2`），因此 pythonnet / clr_loader /
> pywebview 都不再需要，spec 里已显式 `excludes`。
>
> 自检里的 `webview2 backend` 项现在直接验证两件事：`WebView2Loader.dll` 能否定位、
> WebView2 Runtime 是否装了。这比「模块能否 import」可靠得多。

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

**Q：窗口没弹出来**
看日志 `<数据目录>\logs\videorag.log`（注意：数据目录若被降级，后半段日志在
**新数据目录**下的 `logs/videorag.log`，两处都会写明切换）。按提示区分三种情况：

| 现象 | 真实原因 | 处理 |
|---|---|---|
| 提示「未检测到 WebView2 Runtime」 | 确实没装 | 装 Evergreen 运行时 |
| 提示「已检测到…但无法创建环境」 | **位数不一致**（64 位程序配了 32 位运行时）或安装不完整 | 装 **x64** 版运行时 / 修复安装 |
| 同上，且日志里 `0x80070002` | 运行时目录存在但不可用 | 同上 |

位数是高频坑：`msedgewebview2.exe` 若是 x86 而程序是 x64，**任何** loader 都会
返回 `0x80070002`（看着像「没装」）。现在启动时会比对 PE 头位数并直接跳过
不匹配的目录，弹窗里也会写明。

用 `videorag-cli.exe --self-test` 可看到 `webview2 backend` 那一项——它现在做
**真实的环境创建**，而不是只看版本号。**注意不会再回落到浏览器**——桌面版只有
窗口这一种形态。

**Q：窗口起来了，但一片灰/白，没有内容**
这是 COM 引用计数的坑，已在 `app/webview2_host.py::_addref` 里修掉，改动前务必
读那段注释：WebView2 异步创建回调传进来的 `environment` / `controller` 是**借用
引用**，回调返回后运行时就 Release 了。不先 `AddRef` 直接存下来，浏览器进程会
**干净地退出**（事件日志里连崩溃记录都没有），`Navigate` 仍返回 `S_OK`、但
`get_Source` 永远停在 `about:blank`——表现就是「窗口灰屏、服务端一个请求都没收到」。

**Q：程序在 `C:\workspace\...` 能跑，放到别处就不行（或反过来）**
先看日志里有没有「数据目录 … 不可用」。部分安全软件（如腾讯电脑管家）会拦截
**未签名程序**对用户目录的写入，此时 `%LOCALAPPDATA%\videoRAG` 会写不进去，程序
自动降级到 `<exe 同级>/data` 并在日志里说明。把 `videorag.exe` 加入其信任区即可
恢复默认位置；否则两个位置的数据目录是**两套**（旧库不会自动迁移）。

**Q：窗口是一片纯色（蓝色/深色），但服务正常、日志显示「服务就绪」**
先看 **`msedgewebview2.exe` 进程数是不是 0**——如果是，说明 WebView2 的浏览器
子进程根本没起来，窗口里只有我们设的背景色。

最常见的原因不是代码，而是**程序所在目录带 Low 完整性标签**（`Mandatory Label\
Low Mandatory Level`）。Windows 的完整性机制规定：进程的完整性级别不会高于它
所在可执行文件的目录，所以放在这种目录里的 exe 会被降级成 Low，而 WebView2 自带
沙箱的浏览器进程无法在 Low 父进程下启动。

用 `icacls <目录>` 确认有没有 `Low Mandatory Level`。这种情况常见于：
- DSH / Codex 之类工具的**沙箱工作区**（它会主动给目录打 Low 标签来做写入隔离）；
- 某些企业策略目录。

**解决办法就是把发布包解压到普通目录**（如 `C:\Project\videoRAG`）再运行。
同一份 exe 在 `C:\Project` 下 WebView2 子进程有十几个、页面正常；复制到沙箱工作区
就变成 0 个——这不是版本或代码差异。

**Q：包体积能再小吗？**
已默认排除 faster-whisper 保留档（连带 `ctranslate2` + `av`，省约 **124MB**）。
当前实测：解压 **781MB** / zip **293MB**。

已实施的裁剪（都在 `videorag.spec` 里，带注释）：

| 项 | 省 | 依据 |
|---|---|---|
| `ffprobe.exe` 不随包 | **98MB** | 它只用于取时长；`_probe_duration` 已改为缺失时用 `ffmpeg -i` 的输出解析（有测试覆盖） |
| `cv2/opencv_videoio_ffmpeg*.dll` | **29MB** | 项目没有任何 `cv2.VideoCapture`/`VideoWriter`；抽帧走 ffmpeg 子进程 |
| `pyarrow/plasma` | 0.5MB | Plasma 共享内存存储，未使用 |
| `pywebview` / `pythonnet` / `clr_loader` | ~10MB | 窗口改为直连 WebView2 COM，不再需要 CLR 桥 |

**不能删的**：`pyarrow/arrow_dataset.dll` —— lancedb 的 `common.py` 直接
`import pyarrow.dataset`，删掉整个应用启动即失败（实测过）。`arrow_flight`
保守保留（收益 14MB，但无法确认 lancedb 是否间接用到）。

剩下最大的是 `_lancedb.pyd`（296MB，上游 Rust 扩展固有体积，配置层面无解）。

其余可裁项及代价：

| 策略 | 可省 | 代价 |
|---|---|---|
| 排除视觉旁路（`cv2.pyd` + `rapidocr`） | ~97MB | 失去「无语音视频画面文字识别」（MV / PPT 录屏场景） |
| ffmpeg 改首启下载（不随包） | ~98MB | 首次启动需联网；离线环境不可用 |
| 只保留窗口版 exe | ~26MB | CI 自检与命令行排障失去入口，不推荐 |

**注意：用户数据绝不会进包。** spec 在 `COLLECT` 之后会显式删除产物目录里的
`data/` 与 `logs/`——历史上曾因数据目录降级逻辑，把 399MB 的模型与数据库
打进过发布包。
完整分析与实测数据见 `docs/plans/2026-09-17-windows-desktop-packaging-design.md` §6。

**Q：要不要用 `--onefile`？**
不要。onefile 每次启动都要把约 1GB 解包到临时目录，冷启动不可接受；onedir 只解压一次。

## 许可

`vendor-ffmpeg.ps1` 下载的 Windows 静态构建通常是 GPL 授权（含 libx264 等）。
该脚本只用于**构建时**获取，仓库不提交这些二进制；分发安装包时请一并提供对应许可文本。
