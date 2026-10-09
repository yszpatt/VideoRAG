"""桌面/裸机形态的运行时引导（Windows 桌面包专用，Docker 不经过这里）。

Docker 镜像里 ffmpeg 由 apt 装好、数据目录由 compose 注入 ``DATA_DIR=/data``、
静态资源按包内相对路径找得到，因此**不需要**任何引导。但打包成 Windows 应用后，
这三件事都要在进程启动、``create_app()`` 之前自己解决：

1. **ffmpeg**：``cloud_asr`` / ``vision.keyframes`` / yt-dlp 都按名字调用 ``ffmpeg``，
   依赖 PATH。桌面包把 ffmpeg.exe / ffprobe.exe 放在 ``<bundle>/vendor/ffmpeg``，
   启动时前置插入 PATH——必须早于任何子进程调用。
2. **数据目录**：``Settings.data_dir`` 的默认值在 Windows 上要落到
   ``%LOCALAPPDATA%\\videoRAG``，且要支持便携模式与 CLI 覆盖。
3. **端口**：桌面机上常用端口（尤其 8080）被占的概率不低，需要探测空闲端口而不是硬失败。

约定：本模块**只做环境准备，不 import 应用代码**，因此可以在
``from app.main import ...`` 之前安全调用（避免 settings 在引导前就被实例化）。
"""
from __future__ import annotations

import os
import socket
import sys
from pathlib import Path, PureWindowsPath
from typing import Iterable, Sequence

# 捆绑的 ffmpeg 候选目录（相对 bundle 根）；按顺序取第一个存在的
FFMPEG_SUBDIRS = ("vendor/ffmpeg", "vendor/ffmpeg/bin", "ffmpeg", "ffmpeg/bin", "bin")

# 数据目录环境变量：优先桌面专用名，兼容项目既有约定 DATA_DIR
DATA_DIR_ENV_VARS = ("VIDEORAG_DATA_DIR", "DATA_DIR")

# 默认起始端口：刻意避开 8080——它是各类本地服务最常用的默认端口，冲突率很高。
# 被占用时由 find_free_port 自动顺延，所以这里只是"起点"而非硬约束。
# 注意：Docker 形态不受影响（容器内端口由 Dockerfile CMD 与 compose 映射决定）。
DEFAULT_PORT = 8566
_PORT_SCAN_RANGE = 20  # 从 DEFAULT_PORT 起最多向后探测多少个端口


def bundle_root() -> Path:
    """应用根目录：打包后 = exe 所在目录；源码运行 = 仓库根。

    PyInstaller onedir 形态下 ``sys.executable`` 指向 ``<dist>/videoRAG/videorag.exe``，
    其同级目录就是放 ``vendor/`` 的地方；``sys._MEIPASS`` 指向 ``_internal``，
    那是代码与依赖所在，不适合放用户可见的外部二进制。
    """
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent.parent


def resource_root() -> Path:
    """只读资源根：PyInstaller 下为 ``_internal``（``sys._MEIPASS``），否则同仓库根。"""
    meipass = getattr(sys, "_MEIPASS", None)
    if meipass:
        return Path(meipass)
    return Path(__file__).resolve().parent.parent


def find_ffmpeg_dir(root: Path | None = None) -> Path | None:
    """定位捆绑的 ffmpeg 目录（含 ffmpeg 可执行文件者）。未捆绑时返回 None。"""
    root = root or bundle_root()
    exe = "ffmpeg.exe" if os.name == "nt" else "ffmpeg"
    for sub in FFMPEG_SUBDIRS:
        cand = root / sub
        if (cand / exe).is_file():
            return cand
    # 兜底：环境变量显式指定
    override = os.environ.get("VIDEORAG_FFMPEG_DIR")
    if override and (Path(override) / exe).is_file():
        return Path(override)
    return None


def apply_vendor_path(root: Path | None = None) -> str | None:
    """把捆绑的 ffmpeg 目录前置插入 PATH（幂等）。

    返回实际注入的目录（未找到捆绑时返回 None，此时若系统 PATH 里已有 ffmpeg
    则功能照常可用，属于「用户自己装了」的合理场景）。

    **必须在任何 ffmpeg / yt-dlp 子进程启动之前调用**：二者都从 ``os.environ``
    取 PATH，晚于首次调用再注入就来不及了。
    """
    ff = find_ffmpeg_dir(root)
    if ff is None:
        return None
    entry = str(ff)
    parts = [p for p in os.environ.get("PATH", "").split(os.pathsep) if p]
    if entry not in parts:
        parts.insert(0, entry)
        os.environ["PATH"] = os.pathsep.join(parts)
    return entry


def _normalize_path(path: str) -> str:
    """展开 ``~``；相对路径转绝对，**绝对路径原样保留**。

    刻意不调用 ``Path.resolve()``：Windows 上它会给 POSIX 风格的绝对路径加上盘符
    （``/data`` → ``C:\\data``），把容器语义的字符串改形。这个值会写进
    ``DATA_DIR`` 并在跨形态共享数据目录时被比较，保持原样最不容易出意外。
    """
    expanded = os.path.expanduser(path)
    return expanded if os.path.isabs(expanded) else os.path.abspath(expanded)


def windows_default_data_dir(env: dict[str, str]) -> str:
    """Windows 默认数据目录：``%LOCALAPPDATA%\\videoRAG``（无则退回用户主目录）。

    用 ``PureWindowsPath`` 而非 ``Path``：纯路径对象在**任何平台上**都能构造
    并给出一致的 Windows 字符串，因此本函数与它的测试不依赖运行平台
    （``Path`` 会按 ``os.name`` 选择 flavour，在 POSIX 上构造 Windows 路径会
    直接抛 UnsupportedOperation）。同一份默认值逻辑在 ``app/config.py``
    的 ``_default_data_dir`` 里有一份镜像实现（该模块不能 import 应用代码）。
    """
    base = env.get("LOCALAPPDATA") or str(Path.home())
    return str(PureWindowsPath(base) / "videoRAG")


def resolve_data_dir(cli_value: str | None = None, env: dict[str, str] | None = None) -> str:
    """解析数据根目录，优先级：CLI > VIDEORAG_DATA_DIR/DATA_DIR > 便携模式 > 平台默认。

    便携模式（``VIDEORAG_PORTABLE=1``）：用 exe 同级 ``./data``，放进 U 盘即可带走。
    """
    env = os.environ if env is None else env
    if cli_value:
        return _normalize_path(cli_value)
    for key in DATA_DIR_ENV_VARS:
        if env.get(key):
            return _normalize_path(env[key])
    if env.get("VIDEORAG_PORTABLE") not in (None, "", "0", "false", "False"):
        return _normalize_path(str(bundle_root() / "data"))
    # 与 app/config.py 的 _default_data_dir 保持一致（此处不 import 应用代码）
    if os.name == "nt":
        return _normalize_path(windows_default_data_dir(env))
    return "/data"


def is_port_free(port: int, host: str = "127.0.0.1") -> bool:
    """探测端口是否可绑定（只测 bind，不 listen 时长）。"""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        try:
            s.bind((host, port))
        except OSError:
            return False
    return True


def find_free_port(preferred: int = DEFAULT_PORT, host: str = "127.0.0.1",
                   scan: int = _PORT_SCAN_RANGE) -> int:
    """从 preferred 起找一个可绑定端口；全被占则交给系统分配临时端口。"""
    for port in range(preferred, preferred + scan):
        if is_port_free(port, host):
            return port
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind((host, 0))
        return int(s.getsockname()[1])


def find_static_dir(cli_value: str | None = None) -> str | None:
    """定位前端构建产物 ``web/dist``。

    PyInstaller onedir 下 app 包位于 ``_internal/app``，因此
    ``app/main.py`` 的默认推导（``<app 包上级>/web/dist``）恰好指向
    ``_internal/web/dist``——与 spec 里的 datas 目标一致，无需额外处理。
    此处只是把该路径显式化，并支持 CLI 覆盖。
    """
    if cli_value:
        p = Path(cli_value).expanduser()
        return str(p) if p.is_dir() else None
    for root in (resource_root(), bundle_root()):
        cand = root / "web" / "dist"
        if (cand / "index.html").is_file():
            return str(cand)
    return None


# 应用图标候选相对路径：打包后由 spec 放到 _internal/assets，源码运行时在 packaging/logo
APP_ICON_CANDIDATES = ("assets/videorag.ico", "packaging/logo/videorag.ico")


def find_app_icon() -> Path | None:
    """定位 .ico 应用图标（供窗口与任务栏使用）。

    **为什么必须显式设置**：PyInstaller 的 ``icon=`` 只把图标写进 exe 的 PE 资源，
    那只决定资源管理器/桌面快捷方式显示的图标；而**窗口与任务栏**的图标要由程序在
    创建窗口时自己设定（Windows 的 ``WM_SETICON``）。不设的话，窗口会顶着解释器的
    默认图标——即使 exe 的资源图标已经完全正确。
    """
    for root in (resource_root(), bundle_root()):
        for rel in APP_ICON_CANDIDATES:
            candidate = root / rel
            if candidate.is_file():
                return candidate
    return None


def bootstrap(
    data_dir: str | None = None,
    *,
    use_hf_mirror: bool = False,
    extra_path: Sequence[str] = (),
) -> dict:
    """一站式引导：注入 ffmpeg PATH → 固化 DATA_DIR → 可选 HF 镜像。

    返回一份「实际生效了什么」的摘要，供入口打印，便于用户排查。

    **刻意不设置 HF_HOME / XDG_CACHE_HOME**：fastembed 走显式 ``cache_dir=<data>/models``，
    模型落点与 Docker 形态保持一致（同一份数据目录可两形态互换使用）；额外改
    HF_HOME 只会引入两个形态间的差异，得不偿失。
    """
    ff_dir = apply_vendor_path()
    for p in extra_path:
        parts = [x for x in os.environ.get("PATH", "").split(os.pathsep) if x]
        if p and p not in parts:
            parts.insert(0, p)
            os.environ["PATH"] = os.pathsep.join(parts)

    resolved = resolve_data_dir(data_dir)
    # 让 Settings（pydantic-settings）读到同一份路径；环境变量优先于 .env 与默认值。
    # 不在此设置 COOKIE_DIR：Settings 内由 data_dir 派生（<data_dir>/cookies），
    # 保持「一个数据根」的单一真相源；用户显式给了 COOKIE_DIR 时派生逻辑不覆盖它。
    os.environ["DATA_DIR"] = resolved

    hf_endpoint = ""
    if use_hf_mirror and not os.environ.get("HF_ENDPOINT"):
        hf_endpoint = "https://hf-mirror.com"
        os.environ["HF_ENDPOINT"] = hf_endpoint

    Path(resolved).mkdir(parents=True, exist_ok=True)

    return {
        "data_dir": resolved,
        "ffmpeg_dir": ff_dir,
        "ffmpeg_available": _which_ffmpeg() is not None,
        "hf_endpoint": hf_endpoint or os.environ.get("HF_ENDPOINT", ""),
        "frozen": bool(getattr(sys, "frozen", False)),
    }


def _which_ffmpeg() -> str | None:
    from shutil import which

    return which("ffmpeg")


def format_summary(info: dict) -> str:
    """把 bootstrap 结果渲染成可读的多行摘要（入口启动时打印）。"""
    lines = [
        f"  data dir : {info.get('data_dir')}",
        f"  ffmpeg   : {info.get('ffmpeg_dir') or '(not bundled)'}"
        f"{' [PATH ok]' if info.get('ffmpeg_available') else ' [MISSING]'}",
    ]
    backend = info.get("asr_backend")
    if backend:
        desc = "in-process sherpa-onnx" if backend == "inproc" else "local endpoint"
        lines.append(f"  asr      : {backend} ({desc})")
    if info.get("hf_endpoint"):
        lines.append(f"  hf mirror: {info['hf_endpoint']}")
    if info.get("frozen"):
        lines.append("  mode     : packaged")
    return "\n".join(lines)


def iter_candidate_ports(preferred: int, count: int = _PORT_SCAN_RANGE) -> Iterable[int]:
    """暴露候选端口序列，便于测试与日志说明。"""
    return range(preferred, preferred + count)
