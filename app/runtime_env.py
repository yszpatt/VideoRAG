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

import logging
import os
import socket
import sys
from pathlib import Path, PureWindowsPath
from typing import Iterable, Sequence

# 本模块的日志器：数据目录/临时目录的降级决定必须留痕，否则用户只看到
# 「起不来」，不知道程序把数据放到了别处。
log = logging.getLogger("videorag.runtime")

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


#: 最近一次目录探测失败的原因（路径 → 异常描述）。
#: 单独留一份是为了让「降级到备选目录」的日志能说明**为什么**首选目录不可用：
#: 只写「不可用」会让人以为是磁盘权限问题，实际常见原因是安全软件拦截。
_PROBE_FAILURES: dict[str, str] = {}


def _dir_is_usable(path: str) -> bool:
    """目录是否可用：能建子目录**且**能建文件。

    两者都要测：有些 ACL 允许建目录却禁止建文件，而应用要往里写 SQLite、
    LanceDB、日志。任一失败即视为不可用。
    """
    p = Path(path)
    key = os.path.abspath(path)
    try:
        p.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        _PROBE_FAILURES[key] = f"{type(e).__name__}: {e}"
        return False
    probe_dir = p / f".videorag-data-probe-{os.getpid()}"
    probe_file = p / f".videorag-data-probe-{os.getpid()}.tmp"
    try:
        probe_dir.mkdir()
        probe_dir.rmdir()
        probe_file.write_bytes(b"ok")
        probe_file.unlink(missing_ok=True)
    except OSError as e:
        _PROBE_FAILURES[key] = f"{type(e).__name__}: {e}"
        try:
            probe_dir.rmdir()
        except OSError:
            pass
        try:
            probe_file.unlink(missing_ok=True)
        except OSError:
            pass
        return False
    _PROBE_FAILURES.pop(key, None)
    return True


def ensure_usable_data_dir(preferred: str) -> tuple[str, str]:
    """确保数据目录真的可用；不可用时自动退到备选位置。

    为什么需要
    ----------
    默认数据目录是 ``%LOCALAPPDATA%\\videoRAG``。它在受限公司环境里可能
    **不可写**（组策略限制、杀软接管、ACL 异常）。此时应用会卡在
    LanceDB 建库阶段，表现为「窗口白屏 / 90 秒未就绪」——而真正的原因
    （数据目录不可写）不会出现在任何日志里。

    与临时目录同理：先尽力用首选位置，不可用就退到一个能写的近处，
    并把这个决定明确记下来（启动摘要里的 ``data dir`` 会带出来源）。

    返回 ``(实际使用的数据目录, 命中说明)``。
    """
    candidates: list[tuple[str, str]] = []

    def add(path: str | None, source: str) -> None:
        if not path:
            return
        p = os.path.normcase(os.path.abspath(path))
        if all(os.path.normcase(os.path.abspath(c)) != p for c, _ in candidates):
            candidates.append((os.path.abspath(path), source))

    add(preferred, "preferred")
    # 数据目录不可写时的退路。
    #
    # **exe 同级 ``data`` 刻意排在最后**：把用户数据写进程序安装目录，
    # 一是在重新解压/升级安装包时会连同数据一起丢，二是如果这个目录正是
    # 打包输出目录，几百 MB 的模型与数据库会被打进发布包（实测发生过）。
    # 只有在用户目录、同级目录、当前目录都不可写时才退到这里。
    pref = Path(preferred)
    add(str(pref.parent / (pref.name + "-data")), "sibling")
    add(str(Path.home() / pref.name), "home")
    add(str(Path.cwd() / pref.name), "cwd")
    add(str(bundle_root() / "data"), "bundle/data")

    tried: list[str] = []
    for path, source in candidates:
        if _dir_is_usable(path):
            if source != "preferred":
                why = _PROBE_FAILURES.get(os.path.abspath(preferred), "")
                log.warning(
                    "数据目录 %s 不可用（%s），改用 %s（%s）",
                    preferred, why or "原因未知", path, source,
                )
                # 典型现场：非管理员/未签名程序被安全软件（电脑管家、杀软）
                # 禁止写入用户目录，而同一目录用资源管理器/命令行又能写。
                # 光说「不可用」会让人去查磁盘权限，方向就错了。
                if "PermissionError" in why or "拒绝访问" in why or "WinError 5" in why:
                    log.warning(
                        "这多半不是磁盘权限问题，而是安全软件拦截了本程序对用户目录的写入。"
                        "把本程序加入其信任区/白名单，数据目录即可回到 %s。"
                        "（现有数据仍在该目录下，程序只是在用它自己的副本）",
                        preferred,
                    )
            return path, source
        tried.append(path)

    raise RuntimeError(
        "找不到可用的数据目录，无法启动。\n"
        "已尝试：\n  - " + "\n  - ".join(tried) + "\n"
        "解决办法：\n"
        "  1. 用 --data-dir 指定一个可写目录，例如 --data-dir D:\\videorag-data\n"
        "  2. 或设置环境变量 VIDEORAG_DATA_DIR 指向可写目录\n"
        "  3. 检查磁盘空间，以及杀毒软件 / 受控文件夹访问是否拦截了这些目录"
    )


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
    # 数据目录必须真的可用：它要放 SQLite / LanceDB / 日志。若首选位置不可写
    # （受限公司环境、杀软接管、ACL 异常），自动退到备选位置，并把决定记下来。
    try:
        resolved, data_source = ensure_usable_data_dir(resolved)
    except Exception as e:  # noqa: BLE001
        raise RuntimeError(f"数据目录不可用：\n{e}") from e
    # 让 Settings（pydantic-settings）读到同一份路径；环境变量优先于 .env 与默认值。
    # 不在此设置 COOKIE_DIR：Settings 内由 data_dir 派生（<data_dir>/cookies），
    # 保持「一个数据根」的单一真相源；用户显式给了 COOKIE_DIR 时派生逻辑不覆盖它。
    os.environ["DATA_DIR"] = resolved

    hf_endpoint = ""
    if use_hf_mirror and not os.environ.get("HF_ENDPOINT"):
        hf_endpoint = "https://hf-mirror.com"
        os.environ["HF_ENDPOINT"] = hf_endpoint

    Path(resolved).mkdir(parents=True, exist_ok=True)

    # 临时目录必须在**任何 lancedb 调用之前**就绪：Rust 层在建库阶段就要写
    # spill 文件，目录不可写会直接 panic，把整个启动卡死且几乎无日志。
    #
    # 探测失败**不直接终止启动**：先把原因记下来，让应用继续走到
    # ``_preflight_vector_store``，那时能给出更贴近现场的提示（那时才知道
    # 是 lancedb 真的用不了，而不是「找不到临时目录」这种间接描述）。
    temp_dir = ""
    temp_source = ""
    temp_error = ""
    try:
        temp_dir, temp_source = ensure_usable_temp_dir(resolved)
    except Exception as e:  # noqa: BLE001
        temp_error = str(e)

    return {
        "data_dir": resolved,
        "data_source": data_source,
        "temp_dir": temp_dir,
        "temp_source": temp_source,
        "temp_error": temp_error,
        "ffmpeg_dir": ff_dir,
        "ffmpeg_available": _which_ffmpeg() is not None,
        "hf_endpoint": hf_endpoint or os.environ.get("HF_ENDPOINT", ""),
        "frozen": bool(getattr(sys, "frozen", False)),
    }


def ensure_usable_temp_dir(data_dir: str) -> tuple[str, str]:
    """保证进程有一个**确实可写**的临时目录，并把它固定到 TEMP/TMP。

    为什么必须做这件事
    ------------------
    LanceDB 的 Rust 层在 ``connect()`` 阶段就要建 spill 临时目录，用的是
    ``std::env::temp_dir()``（Windows 上读 ``TMP`` → ``TEMP`` → ``USERPROFILE``）。
    一旦该目录不可写，它会**在 Rust 里 panic**：

        thread 'lancedb-tokio-worker' panicked:
        failed to create temp directory for LocalSpillStore:
        ... os { code: 5, kind: PermissionDenied }

    这个 panic 的后果极其难查：``lancedb.connect()`` 抛出的只是
    ``RustPanic: rust future panicked: unknown error``，而且发生在
    ``lifespan`` 启动阶段、uvicorn 还没开始监听，于是桌面端表现为
    **「窗口白屏 / 服务在 90s 内未就绪」**，日志里除了「运行环境」之外一片空白。

    现实中该目录不可写的原因不少：TEMP 指向了只读位置、被安全软件接管、
    装在受限容器里、或用户的临时目录 ACL 异常。

    做法：按候选顺序找第一个「能建目录并写入文件」的位置，把它**写回
    ``TEMP``/``TMP``**，让包括 Rust 在内的所有组件都看到同一个可用目录。
    找不到可写位置时抛错——这比让 lancedb 在 Rust 里 panic 要可诊断得多。

    返回 ``(实际使用的目录, 命中的候选来源说明)``。
    """
    candidates: list[tuple[str, str]] = []

    def add(path: str | None, source: str) -> None:
        if not path:
            return
        expanded = os.path.expanduser(path)
        if not os.path.isabs(expanded):
            return
        if all(os.path.normcase(expanded) != os.path.normcase(p) for p, _ in candidates):
            candidates.append((expanded, source))

    # 1) 用户显式指定的优先（VIDEORAG_TEMP_DIR），便于用户自己兜底
    env = os.environ
    add(env.get("VIDEORAG_TEMP_DIR"), "VIDEORAG_TEMP_DIR")
    # 2) 数据目录下的 temp：应用所有数据都落在这里，理论上必然可写
    add(str(Path(data_dir) / "temp"), "data_dir/temp")
    # 3) 数据目录的同级：数据目录本身被锁（如被安全软件接管）时的退路
    parent = Path(data_dir).parent
    if parent.is_dir():
        add(str(parent / (Path(data_dir).name + "-temp")), "data_dir-sibling/temp")
    # 4) 环境变量指定
    add(env.get("TMP"), "TMP")
    add(env.get("TEMP"), "TEMP")
    add(env.get("TMPDIR"), "TMPDIR")
    # 5) 平台默认
    if os.name == "nt":
        add(env.get("LOCALAPPDATA") and str(Path(env["LOCALAPPDATA"]) / "Temp"), "LOCALAPPDATA/Temp")
        add(env.get("USERPROFILE") and str(Path(env["USERPROFILE"]) / "AppData" / "Local" / "Temp"),
            "USERPROFILE/AppData/Local/Temp")
        add(r"C:\Windows\Temp", r"C:\Windows\Temp")
        add(r"C:\Temp", r"C:\Temp")
        # 6) exe 同级：便携模式/解压即用的场景下通常可写
        try:
            from app import runtime_env as _self

            add(str(_self.bundle_root() / "temp"), "bundle/temp")
        except Exception:  # noqa: BLE001
            pass
    else:
        add("/tmp", "/tmp")
        add("/var/tmp", "/var/tmp")

    tried: list[str] = []
    for path, source in candidates:
        # 分开记录每一步失败原因：用户看到的「已尝试」列表若只写 PermissionError，
        # 会让人以为是权限问题，实际可能是「目录建不出来」或「磁盘满」。
        try:
            Path(path).mkdir(parents=True, exist_ok=True)
        except OSError as e:
            tried.append(f"{path}（{source}：无法创建目录，{type(e).__name__}）")
            continue
        # 目录已存在即**基本可用**：LanceDB 要做的是在该目录下**创建子目录**
        # （``<temp>/.tmpXXXXXX``），不是写文件本身。有些环境允许建子目录、
        # 却不允许在目录里直接写文件——早前把「写文件失败」直接判死刑，
        # 会误杀这些其实可用的目录。
        probe = Path(path) / f".videorag-temp-probe-{os.getpid()}"
        try:
            probe.write_bytes(b"ok")
        except OSError:
            # 写文件失败：先不放弃，改为验证「能否建子目录」——这才是 lancedb 的真实需求
            sub = Path(path) / f".videorag-tmpdir-probe-{os.getpid()}"
            try:
                sub.mkdir()
                sub.rmdir()
            except OSError as e2:
                tried.append(
                    f"{path}（{source}：无法写入文件且无法创建子目录，{type(e2).__name__}）"
                )
                continue
            # 能建子目录 → 认为可用（Rust 侧就靠建子目录工作）
        finally:
            # 清理失败**不算**失败——文件留着只是占用几百字节，
            # 早前因为 unlink 失败就把好目录判成不可写，是误判的根源。
            try:
                probe.unlink(missing_ok=True)
            except OSError:
                pass
        os.environ["TEMP"] = path
        os.environ["TMP"] = path
        if os.name != "nt":
            os.environ["TMPDIR"] = path
        # 让 Python 自己的 tempfile 也别再走缓存里的旧值
        try:
            import tempfile

            tempfile.tempdir = path
        except Exception:  # noqa: BLE001
            pass
        return path, source

    # 全部候选都不可用：仍然把 TEMP/TMP 指向数据目录下的 temp。
    #
    # 为什么不能直接放弃：LanceDB 的 Rust 层读的是**进程环境变量**里的
    # TMP/TEMP。若保持原样（指向那个完全不可写的系统 TEMP），Rust 一定会
    # panic；指到数据目录下的 temp 至少还有一线机会（数据目录往往是应用
    # 自己能写的，例如放在 D 盘或 U 盘上的便携模式）。
    fallback = str(Path(data_dir) / "temp")
    try:
        Path(fallback).mkdir(parents=True, exist_ok=True)
    except OSError:
        pass
    os.environ["TEMP"] = fallback
    os.environ["TMP"] = fallback
    if os.name != "nt":
        os.environ["TMPDIR"] = fallback
    try:
        import tempfile

        tempfile.tempdir = fallback
    except Exception:  # noqa: BLE001
        pass

    raise RuntimeError(
        "找不到可写的临时目录，无法启动。\n"
        "LanceDB 需要在该目录下创建 spill 临时文件。\n"
        "已尝试：\n  - " + "\n  - ".join(tried) + "\n"
        "解决办法（任选其一）：\n"
        "  1. 新建一个空目录（如 D:\\videorag-temp），设置环境变量\n"
        "     VIDEORAG_TEMP_DIR 指向它，再重新启动本程序\n"
        "  2. 或把 TEMP / TMP 指向可写目录后重新启动\n"
        "  3. 检查磁盘空间，以及杀毒软件 / 受控文件夹访问是否拦截了这些目录\n"
        "  4. 若数据目录本身也不可写，请检查 "
        f"{data_dir} 的权限"
    )


def _which_ffmpeg() -> str | None:
    from shutil import which

    return which("ffmpeg")


def format_summary(info: dict) -> str:
    """把 bootstrap 结果渲染成可读的多行摘要（入口启动时打印）。"""
    lines = [
        f"  data dir : {info.get('data_dir')}"
        + (f" [{info['data_source']}]" if info.get("data_source") else ""),
        f"  ffmpeg   : {info.get('ffmpeg_dir') or '(not bundled)'}"
        f"{' [PATH ok]' if info.get('ffmpeg_available') else ' [MISSING]'}",
    ]
    # 临时目录显式打出来：LanceDB 在它不可写时会 panic 且几乎不留日志，
    # 把「实际用的是哪个目录、来自哪条候选」写进启动摘要，排查时一眼可见。
    if info.get("temp_dir"):
        lines.append(f"  temp dir : {info['temp_dir']} [{info.get('temp_source', '')}]")
    elif info.get("temp_error"):
        lines.append("  temp dir : (无可用位置——见下方警告)")
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
