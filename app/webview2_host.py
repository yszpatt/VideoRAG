"""WebView2 原生窗口宿主（纯 ctypes + COM，**不依赖 pythonnet / .NET / pywebview**）。

为什么要自己写这一层
--------------------
原实现用 pywebview，Windows 上它走 ``webview.platforms.winforms`` → ``pythonnet``
→ .NET Framework 加载 CLR。这条链路在真机上会以两种方式失败：

1. ``clr_loader`` 在 Windows 上默认选 **netfx**，用 ``Assembly.LoadFrom`` 加载
   ``Python.Runtime.dll``。若该路径被 .NET Framework 判定为「来自网络位置」，
   CAS 策略直接拒绝（``0x80131515`` / ``loadFromRemoteSources``），报
   ``Failed to resolve Python.Runtime.Loader.Initialize``——与机器上装没装
   .NET Desktop Runtime 完全无关。
2. 改走 CoreCLR 同样不行：``System.Windows.Forms.ContextMenu`` 在 .NET
   Core/5+ 已被移除，WinForms 版 WebView2 控件在 CoreCLR 下无法实例化
   （``TypeLoadException``）。

两条路都堵死，所以这里直接调 WebView2 官方 Win32/COM API：用
``WebView2Loader.dll`` 的 ``CreateCoreWebView2EnvironmentWithOptions`` 建环境，
再用 ``ICoreWebView2Environment::CreateCoreWebView2Controller`` 建控制器，
把控制器挂到自建的 Win32 窗口上。全程只有 ``ctypes`` 与 COM vtable 调用，
**不需要任何 .NET 运行时**，因此不会再被 CAS / GAC / Desktop Runtime 影响。

唯一前提：目标机器装有 **WebView2 Runtime**。Windows 11 全系内置，Windows 10
多数随 Edge 装上；缺失时本模块给出可读错误（含下载地址）。

线程模型
--------
Win32 消息循环必须在**主线程**跑（与 uvicorn 的后台线程互不冲突），
因此 :func:`run_window` 会阻塞，直到用户关闭窗口。

关于 vtable 下标
----------------
下面所有 ``[n]`` 下标都是**含 IUnknown 三个方法在内**的完整下标
（QueryInterface=0、AddRef=1、Release=2），逐条对照
``Microsoft.Web.WebView2.Core.dll`` 里 ``Microsoft.Web.WebView2.Core.Raw``
命名空间的接口声明得到，不可凭记忆改动。
"""
from __future__ import annotations

import ctypes
import logging
import os
import sys
import threading
import time
from ctypes import POINTER, WINFUNCTYPE, c_int, c_ssize_t, c_uint, c_void_p, c_wchar_p
from pathlib import Path

log = logging.getLogger("videorag.webview2")

# ---------------------------------------------------------------------------
# 常量
# ---------------------------------------------------------------------------

SW_SHOWNORMAL = 1
WS_OVERLAPPEDWINDOW = 0x00CF0000
WS_CLIPCHILDREN = 0x02000000
CW_USEDEFAULT = 0x80000000
WM_DESTROY = 0x0002
WM_SIZE = 0x0005
WM_CLOSE = 0x0010
WM_GETMINMAXINFO = 0x0024
IDC_ARROW = 32512
SM_CXICON, SM_CYICON = 11, 12
SM_CXSMICON, SM_CYSMICON = 49, 50
PM_REMOVE = 0x0001
GWLP_WNDPROC = -4

# 自定义窗口消息：由它把「导航」推迟到消息循环内部执行
# （WebView2 只在循环运行时才接受 Navigate，见 WebView2Window._run_inner）
WM_APP_NAVIGATE = 0x8001

S_OK = 0
E_NOINTERFACE = 0x80004002

# WebView2 异步创建的回调超时（秒）。
#
# 为什么是 25 而不是 60：回调迟迟不来时，窗口已经显示出来了（深色空白），
# 用户看到的是「程序卡死」。实测正常的 Runtime 建环境是亚秒到数秒级，
# 25s 足够覆盖冷机首启；再等下去只是把「报错」拖成「假死」。
_HANDLER_TIMEOUT = 25.0

# 判定「运行时目录可用」的探测超时（秒）。首启（尤其刚重装 Runtime、
# 需要迁移 profile）比日常慢得多，6s 会把它误判成坏目录。
_PROBE_TIMEOUT = 15.0


class Wv2Error(RuntimeError):
    """WebView2 初始化/运行失败（附可操作的原因）。"""


# ---------------------------------------------------------------------------
# ctypes 基础类型
# ---------------------------------------------------------------------------

_HRESULT = c_int
_ULONG = ctypes.c_ulong
WNDPROC = WINFUNCTYPE(c_ssize_t, c_void_p, c_uint, ctypes.c_size_t, c_ssize_t)


class GUID(ctypes.Structure):
    _fields_ = [
        ("Data1", ctypes.c_uint32),
        ("Data2", ctypes.c_uint16),
        ("Data3", ctypes.c_uint16),
        ("Data4", ctypes.c_ubyte * 8),
    ]

    def __init__(self, text: str = "") -> None:
        super().__init__()
        if not text:
            return
        p1, p2, p3, p4, p5 = text.strip("{}").split("-")
        self.Data1 = int(p1, 16)
        self.Data2 = int(p2, 16)
        self.Data3 = int(p3, 16)
        rest = p4 + p5
        for i in range(8):
            self.Data4[i] = int(rest[i * 2 : i * 2 + 2], 16)

    def __str__(self) -> str:  # pragma: no cover - 仅日志/测试
        d4 = "".join(f"{b:02X}" for b in self.Data4)
        return f"{{{self.Data1:08X}-{self.Data2:04X}-{self.Data3:04X}-{d4[:4]}-{d4[4:]}}}"


def _guid_eq(a: GUID, b: GUID) -> bool:
    return (
        a.Data1 == b.Data1
        and a.Data2 == b.Data2
        and a.Data3 == b.Data3
        and bytes(a.Data4) == bytes(b.Data4)
    )


# 接口 IID：取自 Microsoft.Web.WebView2.Core.Raw 各类型的 Guid 特性
IID_IUnknown = GUID("00000000-0000-0000-C000-000000000046")
IID_ICoreWebView2Environment = GUID("B96D755E-0319-4E92-A296-23436F46A1FC")
IID_ICoreWebView2Controller = GUID("4D00C0D1-9434-4EB6-8078-8697A560334F")
IID_ICoreWebView2 = GUID("76ECEACB-0462-4D94-AC83-423A6793775E")
IID_EnvCompletedHandler = GUID("4E8A3389-C9D8-4BD2-B6B5-124FEE6CC14D")
IID_CtrlCompletedHandler = GUID("6C4819F3-C9B7-4260-8127-C9F5BDE7F68C")


class _RECT(ctypes.Structure):
    _fields_ = [
        ("left", ctypes.c_long), ("top", ctypes.c_long),
        ("right", ctypes.c_long), ("bottom", ctypes.c_long),
    ]


class _MINMAXINFO(ctypes.Structure):
    _fields_ = [
        ("ptReserved_x", ctypes.c_long), ("ptReserved_y", ctypes.c_long),
        ("ptMaxSize_x", ctypes.c_long), ("ptMaxSize_y", ctypes.c_long),
        ("ptMaxPosition_x", ctypes.c_long), ("ptMaxPosition_y", ctypes.c_long),
        ("ptMinTrackSize_x", ctypes.c_long), ("ptMinTrackSize_y", ctypes.c_long),
        ("ptMaxTrackSize_x", ctypes.c_long), ("ptMaxTrackSize_y", ctypes.c_long),
    ]


class _MSG(ctypes.Structure):
    _fields_ = [
        ("hwnd", c_void_p),
        ("message", c_uint),
        ("wParam", ctypes.c_size_t),
        ("lParam", c_ssize_t),
        ("time", c_uint),
        ("pt_x", ctypes.c_long),
        ("pt_y", ctypes.c_long),
    ]


class WNDCLASSEXW(ctypes.Structure):
    _fields_ = [
        ("cbSize", c_uint),
        ("style", c_uint),
        ("lpfnWndProc", WNDPROC),
        ("cbClsExtra", c_int),
        ("cbWndExtra", c_int),
        ("hInstance", c_void_p),
        ("hIcon", c_void_p),
        ("hCursor", c_void_p),
        ("hbrBackground", c_void_p),
        ("lpszMenuName", c_wchar_p),
        ("lpszClassName", c_wchar_p),
        ("hIconSm", c_void_p),
    ]


# ---------------------------------------------------------------------------
# Win32 / GDI 签名声明
#
# ctypes 默认把参数/返回值当 C int 处理，在 64 位下会把指针截断成 32 位而崩溃，
# 因此每个用到的函数都必须显式声明 argtypes/restype。
# ---------------------------------------------------------------------------

_user32 = ctypes.WinDLL("user32", use_last_error=True)
_kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
_gdi32 = ctypes.WinDLL("gdi32", use_last_error=True)
_ole32 = ctypes.WinDLL("ole32", use_last_error=True)

# COM 线程模型：WebView2 要求在 STA 线程上创建（WinForms/WPF 宿主也都是 STA）。
COINIT_APARTMENTTHREADED = 0x2
COINIT_DISABLE_OLE1DDE = 0x4
RPC_E_CHANGED_MODE = 0x80010106


def _co_initialize() -> None:
    """把当前线程初始化为 STA。

    WebView2 的 COM 对象要求调用线程已初始化 COM，否则
    ``CreateCoreWebView2EnvironmentWithOptions`` 的回调直接返回
    ``0x800401F0``（CO_E_NOTINITIALIZED）。STA 是 WebView2 的标准宿主模型；
    若线程已以 MTA 初始化（``RPC_E_CHANGED_MODE``），不强行改变——
    那通常意味着宿主另有约定，此时仍可尝试创建。
    """
    hr = _ole32.CoInitializeEx(None, COINIT_APARTMENTTHREADED | COINIT_DISABLE_OLE1DDE)
    if hr < 0 and (hr & 0xFFFFFFFF) != RPC_E_CHANGED_MODE:
        raise Wv2Error(f"CoInitializeEx 失败（0x{hr & 0xFFFFFFFF:08X}）")


def _co_uninitialize() -> None:
    try:
        _ole32.CoUninitialize()
    except Exception:  # noqa: BLE001
        pass


def _declare_win32() -> None:
    u, k, g = _user32, _kernel32, _gdi32

    k.GetModuleHandleW.argtypes = [c_wchar_p]
    k.GetModuleHandleW.restype = c_void_p

    u.RegisterClassExW.argtypes = [POINTER(WNDCLASSEXW)]
    u.RegisterClassExW.restype = ctypes.c_ushort
    u.CreateWindowExW.argtypes = [
        ctypes.c_uint, c_wchar_p, c_wchar_p, ctypes.c_uint,
        c_int, c_int, c_int, c_int, c_void_p, c_void_p, c_void_p, c_void_p,
    ]
    u.CreateWindowExW.restype = c_void_p
    u.DefWindowProcW.argtypes = [c_void_p, c_uint, ctypes.c_size_t, c_ssize_t]
    u.DefWindowProcW.restype = c_ssize_t
    u.DestroyWindow.argtypes = [c_void_p]
    u.DestroyWindow.restype = ctypes.c_bool
    u.ShowWindow.argtypes = [c_void_p, c_int]
    u.ShowWindow.restype = ctypes.c_bool
    u.UpdateWindow.argtypes = [c_void_p]
    u.UpdateWindow.restype = ctypes.c_bool
    u.GetClientRect.argtypes = [c_void_p, POINTER(_RECT)]
    u.GetClientRect.restype = ctypes.c_bool
    u.AdjustWindowRectEx.argtypes = [POINTER(_RECT), ctypes.c_uint, ctypes.c_bool, ctypes.c_uint]
    u.AdjustWindowRectEx.restype = ctypes.c_bool
    u.GetMessageW.argtypes = [POINTER(_MSG), c_void_p, c_uint, c_uint]
    u.GetMessageW.restype = c_int
    u.PeekMessageW.argtypes = [POINTER(_MSG), c_void_p, c_uint, c_uint, c_uint]
    u.PeekMessageW.restype = ctypes.c_bool
    u.TranslateMessage.argtypes = [POINTER(_MSG)]
    u.TranslateMessage.restype = ctypes.c_bool
    u.DispatchMessageW.argtypes = [POINTER(_MSG)]
    u.DispatchMessageW.restype = c_ssize_t
    u.PostQuitMessage.argtypes = [c_int]
    u.PostQuitMessage.restype = None
    u.PostMessageW.argtypes = [c_void_p, c_uint, ctypes.c_size_t, c_ssize_t]
    u.PostMessageW.restype = ctypes.c_bool
    u.LoadImageW.argtypes = [c_void_p, c_wchar_p, c_uint, c_int, c_int, c_uint]
    u.LoadImageW.restype = c_void_p
    u.LoadCursorW.argtypes = [c_void_p, c_void_p]
    u.LoadCursorW.restype = c_void_p
    u.SetWindowTextW.argtypes = [c_void_p, c_wchar_p]
    u.SetWindowTextW.restype = ctypes.c_bool
    u.GetSystemMetrics.argtypes = [c_int]
    u.GetSystemMetrics.restype = c_int
    u.SetWindowLongPtrW.argtypes = [c_void_p, c_int, c_void_p]
    u.SetWindowLongPtrW.restype = c_void_p
    u.MessageBoxW.argtypes = [c_void_p, c_wchar_p, c_wchar_p, c_uint]
    u.MessageBoxW.restype = c_int

    g.CreateSolidBrush.argtypes = [ctypes.c_uint]
    g.CreateSolidBrush.restype = c_void_p


_declare_win32()

# RegisterClassExW 需要的窗口类名；进程内只注册一次
_WINDOW_CLASS = "VideoRAGWebView2Window"
_class_registered = False


# ---------------------------------------------------------------------------
# COM vtable 调用
# ---------------------------------------------------------------------------


def _as_ptr(ptr) -> int:
    """把一个接口指针统一成 Python int。

    ``ctypes.cast(x, T)`` 的语义陷阱：当 ``x`` 是 ``c_void_p`` **实例**时，
    cast 取的是「该 ctypes 容器自身的地址」，而不是容器里保存的指针值——
    于是后续解引用拿到的就是垃圾。所有接口指针都在这里归一成 int，
    调用方一律用 ``ctypes.cast(int, ...)``，语义才明确。
    """
    if ptr is None:
        return 0
    if isinstance(ptr, ctypes.c_void_p):
        return ptr.value or 0
    return int(ptr)


def _vtbl_method(ptr, index: int, restype, *argtypes):
    """取出接口指针 ``ptr`` 的 vtable[index]，返回已绑定 this 的可调用对象。"""
    addr = _as_ptr(ptr)
    if not addr:
        raise Wv2Error("空接口指针")
    # COM 对象首字段即 vtable 指针
    vtbl = ctypes.cast(addr, POINTER(c_void_p))[0]
    if not vtbl:
        raise Wv2Error(f"接口 {addr:#x} 的 vtable 为空")
    fn = ctypes.cast(vtbl, POINTER(c_void_p))[index]
    if not fn:
        raise Wv2Error(f"vtable[{index}] 为空")
    return WINFUNCTYPE(restype, c_void_p, *argtypes)(fn)


def _qi(ptr, iid: GUID) -> c_void_p | None:
    """QueryInterface，拿不到返回 None（不抛异常）。"""
    try:
        fn = _vtbl_method(ptr, 0, _HRESULT, POINTER(GUID), POINTER(c_void_p))
    except Wv2Error:
        return None
    out = c_void_p()
    hr = fn(_as_ptr(ptr), ctypes.byref(iid), ctypes.byref(out))
    return out if (hr >= 0 and out) else None


class _ComHandler:
    """把 Python 回调包装成 COM 对象（IUnknown + 一个 Invoke）。

    WebView2 的异步创建 API 要求传入「完成回调」对象。实现要点：

    * ``QueryInterface`` 必须应答自身 handler IID 与 IUnknown——WebView2 会按
      不同 IID 查询，答错则初始化失败；
    * 回调在 WebView2 的线程上触发，因此结果写入字段 + ``Event`` 唤醒调用方；
    * ctypes 函数指针必须存活，否则被 GC 回收后回调即崩（见 ``_keepalive``）。
    """

    #: 子类声明：本 handler 认可的 IID（不含 IUnknown）
    _iids: tuple[GUID, ...] = ()

    def __init__(self) -> None:
        self._refcount = 1
        self.result: int | None = None
        self.error: str | None = None
        self.done = threading.Event()

        # Invoke(this, HRESULT, void*)：两个 handler 的签名一致，
        # 因此用统一原型即可（真实类型由第二参数区分）。
        self._invoke_cb = WINFUNCTYPE(_HRESULT, c_void_p, _HRESULT, c_void_p)(self._invoke)
        self._qi_cb = WINFUNCTYPE(_HRESULT, c_void_p, POINTER(GUID), POINTER(c_void_p))(self._qi_impl)
        self._addref_cb = WINFUNCTYPE(_ULONG, c_void_p)(self._addref_impl)
        self._release_cb = WINFUNCTYPE(_ULONG, c_void_p)(self._release_impl)

        self._vtbl = (c_void_p * 4)(
            ctypes.cast(self._qi_cb, c_void_p),
            ctypes.cast(self._addref_cb, c_void_p),
            ctypes.cast(self._release_cb, c_void_p),
            ctypes.cast(self._invoke_cb, c_void_p),
        )
        # COM 对象 = 一个指针大小的内存单元，内容为 vtable 地址。
        # 用单元素 c_void_p 数组承载这块内存：可写、地址稳定、由 self 持有引用。
        self._cell = (c_void_p * 1)(ctypes.cast(self._vtbl, c_void_p))
        self._obj_addr = ctypes.cast(self._cell, c_void_p).value or 0

    # --- IUnknown ---
    def _qi_impl(self, this, riid, ppv):
        try:
            want = ctypes.cast(riid, POINTER(GUID)).contents
            for known in (IID_IUnknown, *self._iids):
                if _guid_eq(want, known):
                    self._refcount += 1
                    ppv[0] = self._obj_addr
                    return S_OK
            ppv[0] = None
            return E_NOINTERFACE
        except Exception:  # noqa: BLE001  回调里绝不能抛
            return E_NOINTERFACE

    def _addref_impl(self, this):
        self._refcount += 1
        return self._refcount

    def _release_impl(self, this):
        self._refcount -= 1
        return max(self._refcount, 0)

    def _invoke(self, this, hr, ptr):
        try:
            if hr < 0:
                self.error = f"WebView2 回调失败（HRESULT=0x{hr & 0xFFFFFFFF:08X}）"
            elif not ptr:
                self.error = "WebView2 回调成功但未返回对象"
            else:
                value = ptr.value if isinstance(ptr, ctypes.c_void_p) else ptr
                self.result = c_void_p(value)
                # 允许子类在**回调线程**内继续调用该对象。这是 WebView2 的硬约束：
                # 环境/控制器都带线程亲和性，跨线程调用一律返回
                # ERROR_WRONG_THREAD（0x802A000C），即使创建与使用都在同一 Python
                # 线程——因为实现上这些对象归属于回调投递所在的公寓。
                self.on_result(value)
        except Exception as e:  # noqa: BLE001
            self.error = f"{type(e).__name__}: {e}"
        finally:
            self.done.set()
        return S_OK

    def on_result(self, ptr_addr: int) -> None:
        """子类覆盖：在回调线程上下文里继续使用该接口指针。"""

    @property
    def pointer(self) -> int:
        """传给 WebView2 的接口指针（int 形式，避免 ctypes 容器语义歧义）。"""
        return self._obj_addr


class _EnvCompletedHandler(_ComHandler):
    """``ICoreWebView2CreateCoreWebView2EnvironmentCompletedHandler``。

    拿到环境后**必须在回调线程里**继续创建控制器并把初始化做完：WebView2 的
    环境与控制器都带线程亲和性，换线程调用任何方法都会得到
    ``ERROR_WRONG_THREAD (0x802A000C)``。因此这里把「后续装配」整体交给回调。
    """

    _iids = (IID_EnvCompletedHandler,)

    def __init__(self, on_env) -> None:  # noqa: ANN001
        super().__init__()
        self._on_env = on_env          # 回调：fn(env_addr) -> None，在回调线程执行
        self.controller: c_void_p | None = None
        self.webview: c_void_p | None = None

    def on_result(self, ptr_addr: int) -> None:
        self._on_env(ptr_addr)


class _CtrlCompletedHandler(_ComHandler):
    """``ICoreWebView2CreateCoreWebView2ControllerCompletedHandler``。"""

    _iids = (IID_CtrlCompletedHandler,)

    def __init__(self, on_ctrl) -> None:  # noqa: ANN001
        super().__init__()
        self._on_ctrl = on_ctrl

    def on_result(self, ptr_addr: int) -> None:
        self._on_ctrl(ptr_addr)


# ---------------------------------------------------------------------------
# WebView2Loader.dll 定位与加载
# ---------------------------------------------------------------------------


def _candidate_loader_dlls() -> list[Path]:
    """WebView2Loader.dll 的候选路径。

    注意两个不同的根：

    * **打包后** loader 由 spec 打进 ``_internal/vendor/webview2``（属于只读资源，
      与 app 代码同处 ``sys._MEIPASS``），所以必须从 ``resource_root()`` 找；
    * **源码运行**时它在仓库根的 ``packaging/vendor/webview2``，或 pywebview
      wheel 自带的 ``lib/runtimes/win-x64/native`` 下。

    历史故障：只查 ``bundle_root()``（exe 同级目录），而 PyInstaller 把 datas 放进
    ``_internal``，于是发布包里永远找不到 loader、窗口一启动就报
    「未找到 WebView2Loader.dll」。两个根都要查。
    """
    from app import runtime_env

    cands: list[Path] = []
    # 1) 打包资源根（_internal）与 bundle 根（exe 同级）都查：不同打包方式落点不同
    for root in (runtime_env.resource_root(), runtime_env.bundle_root()):
        cands += [
            root / "vendor" / "webview2" / "WebView2Loader.dll",
            root / "vendor" / "webview2" / "amd64" / "WebView2Loader.dll",
            root / "vendor" / "webview2" / "win-x64" / "native" / "WebView2Loader.dll",
        ]
    # 2) 源码运行时的仓库路径
    cands.append(
        Path(__file__).resolve().parent.parent
        / "packaging" / "vendor" / "webview2" / "WebView2Loader.dll"
    )
    # 3) 开发环境：pywebview wheel 里自带的那份
    try:
        import webview  # noqa: F401

        cands.append(
            Path(webview.__file__).parent / "lib" / "runtimes" / "win-x64" / "native" / "WebView2Loader.dll"
        )
    except Exception:  # noqa: BLE001  pywebview 是可选的
        pass
    pfx86 = os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)")
    cands.append(Path(pfx86) / "EdgeWebView" / "Application" / "WebView2Loader.dll")
    return cands


def find_loader_dll() -> Path | None:
    for c in _candidate_loader_dlls():
        if c.is_file():
            return c
    return None


def _enumerate_runtime_dirs() -> list[Path]:
    """列出**看起来像** Runtime 的目录（纯枚举，不创建环境、不验证）。

    与 :func:`find_runtime_dir` 分开是必要的：诊断「装了但用不了」时需要知道
    「目录到底在不在」，而这件事不能靠探测——探测失败与目录不存在是两种结论，
    错误提示必须能区分（否则用户会去重装一个其实已经装了的运行时）。

    顺序：``VIDEORAG_WEBVIEW2_DIR`` 显式指定 → 标准安装位置（版本号从高到低）
    → 注册表登记的 location。
    """
    env = os.environ
    cands: list[Path] = []

    override = env.get("VIDEORAG_WEBVIEW2_DIR")
    if override:
        cands.append(Path(override))

    roots = [
        Path(env.get("ProgramFiles(x86)", r"C:\Program Files (x86)"))
        / "Microsoft" / "EdgeWebView" / "Application",
        Path(env.get("ProgramFiles", r"C:\Program Files"))
        / "Microsoft" / "EdgeWebView" / "Application",
    ]
    # 按用户安装（「仅为我安装」）落在 %LOCALAPPDATA% 下，与全机安装是两条路径。
    # 漏掉这一条会得出「明明装好了却找不到目录」的错误结论。
    local = env.get("LOCALAPPDATA")
    if local:
        roots.append(Path(local) / "Microsoft" / "EdgeWebView" / "Application")

    def _version_key(p: Path) -> list[int]:
        return [int(x) if x.isdigit() else 0 for x in p.name.split(".")]

    for root in roots:
        if not root.is_dir():
            continue
        versions = [
            d for d in root.iterdir()
            if d.is_dir() and (d / "msedgewebview2.exe").is_file()
        ]
        versions.sort(key=_version_key, reverse=True)
        cands += versions

    # 注册表登记的 location（可能是父目录，也可能直接是版本目录）
    try:
        import winreg

        key = r"SOFTWARE\WOW6432Node\Microsoft\EdgeUpdate\Clients\{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}"
        for hive in (winreg.HKEY_LOCAL_MACHINE, winreg.HKEY_CURRENT_USER):
            try:
                with winreg.OpenKey(hive, key) as k:
                    loc = winreg.QueryValueEx(k, "location")[0]
            except OSError:
                continue
            base = Path(loc)
            if (base / "msedgewebview2.exe").is_file():
                cands.append(base)
            if base.is_dir():
                subs = [
                    d for d in base.iterdir()
                    if d.is_dir() and (d / "msedgewebview2.exe").is_file()
                ]
                subs.sort(key=_version_key, reverse=True)
                cands += subs
    except Exception:  # noqa: BLE001
        pass

    # 去重并保持顺序
    seen: set[str] = set()
    ordered: list[Path] = []
    for c in cands:
        key_s = os.path.normcase(str(c))
        if key_s in seen:
            continue
        seen.add(key_s)
        ordered.append(c)
    return ordered


#: 当前进程的位数。WebView2 运行时与宿主必须位数一致：x64 宿主**不能**使用
#: 只装了 x86 的运行时，此时任何 Loader 都会返回 0x80070002（ERROR_FILE_NOT_FOUND），
#: 看起来像「没装 Runtime」——这是真实踩过的坑（手动重装时装成了 x86 包）。
_HOST_ARCH = "x64" if ctypes.sizeof(c_void_p) == 8 else "x86"

_PE_MACHINE_NAMES = {0x8664: "x64", 0x14C: "x86", 0xAA64: "arm64"}


def _pe_machine(path: Path) -> int | None:
    """读 PE 头的 Machine 字段（判断某个 exe/dll 是 32 位还是 64 位）。"""
    try:
        with path.open("rb") as f:
            if f.read(2) != b"MZ":
                return None
            f.seek(0x3C)
            raw = f.read(4)
            if len(raw) != 4:
                return None
            offset = int.from_bytes(raw, "little")
            f.seek(offset)
            if f.read(4) != b"PE\0\0":
                return None
            mach = f.read(2)
            return int.from_bytes(mach, "little") if len(mach) == 2 else None
    except OSError:
        return None


def runtime_arch(folder: Path) -> str | None:
    """该 Runtime 目录的位数（按 ``msedgewebview2.exe`` 的 PE 头判断）。"""
    machine = _pe_machine(folder / "msedgewebview2.exe")
    return _PE_MACHINE_NAMES.get(machine) if machine is not None else None


def find_runtime_dir() -> Path | None:
    """定位一个**真能创建环境**的 WebView2 Runtime 目录。

    为什么要自己验证
    --------------
    光看目录存在是不够的。真实故障现场：机器上同时存在 152.x（可用）与 155.x
    （重装时安装器报「系统已安装」而残留的半成品，缺关键文件）。若按版本号
    「挑最大的」，就会选中那个坏目录，然后得到
    ``0x80070002 (ERROR_FILE_NOT_FOUND)``——看起来像「没装 Runtime」，
    实际上只是挑错了目录。

    因此这里逐个候选做一次**真实的环境创建探测**（带最小回调），
    取第一个能成功创建的。探测结果会缓存，避免每次启动都重试一遍。
    """
    global _runtime_dir_cache
    if _runtime_dir_cache is not None:
        # 缓存里存的是字符串（便于写文件），对外必须统一成 Path——
        # 否则调用方拿到的类型随「本次是否命中缓存」而变化（真实踩过）。
        return Path(_runtime_dir_cache) if _runtime_dir_cache else None

    ordered = _enumerate_runtime_dirs()

    for c in ordered:
        try:
            if not (c / "msedgewebview2.exe").is_file():
                continue
        except OSError:
            continue
        # 位数不一致的运行时**永远**建不出环境（x64 宿主不能用 x86 运行时），
        # 探测它只会白等超时，还容易被误判成「目录损坏」。
        arch = runtime_arch(c)
        if arch is not None and arch != _HOST_ARCH:
            log.warning(
                "跳过 WebView2 Runtime %s：它是 %s 版，本程序是 %s 版",
                c.name, arch, _HOST_ARCH,
            )
            continue
        # 优先参考上次成功的记录，避免每次都从头探测（探测要建一次环境，
        # 有问题的目录每个要等到超时，会显著拖慢启动）。
        if _cached_choice_valid(c):
            _runtime_dir_cache = str(c)
            return c
        if _probe_runtime_dir(c):
            _runtime_dir_cache = str(c)
            _remember_choice(c)
            if len(ordered) > 1:
                log.info("WebView2 Runtime 选用 %s（已排除不可用版本）", c.name)
            return c

    _runtime_dir_cache = ""
    return None


_runtime_dir_cache: str | None = None


def _cache_file() -> Path | None:
    """缓存上次验证可用的 Runtime 目录，避免每次启动都做环境探测。"""
    try:
        from app import runtime_env

        base = Path(runtime_env.resolve_data_dir(None)) / "webview2"
        base.mkdir(parents=True, exist_ok=True)
        return base / "runtime_choice.txt"
    except Exception:  # noqa: BLE001
        return None


def _cached_choice_valid(cand: Path) -> bool:
    """缓存命中且目录仍存在 → 直接采用（不重复探测）。"""
    f = _cache_file()
    if f is None or not f.is_file():
        return False
    try:
        cached = f.read_text(encoding="utf-8").strip()
    except OSError:
        return False
    if not cached:
        return False
    try:
        return os.path.normcase(cached) == os.path.normcase(str(cand))
    except OSError:
        return False


def _remember_choice(cand: Path) -> None:
    f = _cache_file()
    if f is None:
        return
    try:
        f.write_text(str(cand), encoding="utf-8")
    except OSError:
        pass


def _probe_runtime_dir(folder: Path | None, timeout: float = _PROBE_TIMEOUT) -> bool:
    """真实创建一次 WebView2 环境，判断该 Runtime 是否可用。

    这是唯一可靠的判据：目录存在、甚至版本号更高，都不代表它能跑起来
    （实测 155.x 的残留半成品目录会返回 ``0x80070002``）。

    ``folder=None`` 表示交给 WebView2Loader 自动发现——按用户安装的运行时
    常常不在标准 System 路径下，那种情况下「枚举不到目录」并不等于不可用。

    超时给 15 秒（``_PROBE_TIMEOUT``）：日常建环境是亚秒级，但**刚重装 Runtime
    后的首启**要迁移 profile，实测会慢到十几秒——早先给 6 秒，把刚装好的运行时
    误判成坏目录，于是「明明装了却报未安装」。超时后只是跳过该目录，不会阻塞启动。

    注意：回调由**发起调用的线程**投递，因此消息泵必须在本线程跑；
    放到子线程里泵是没有用的。
    """
    try:
        lib = _load_loader()
    except Wv2Error:
        return False

    # 必须在**当前线程**初始化 COM：探测会在自检线程、日志线程等各种地方被调用，
    # 而那些线程通常没被 CoInitialize 过，此时 CreateCoreWebView2EnvironmentWithOptions
    # 直接返回 0x800401F0（CO_E_NOTINITIALIZED），于是「运行时明明装好了」也被判成
    # 不可用。这是真机上出现过的误判，代价是窗口根本不去尝试创建。
    try:
        _co_initialize()
    except Wv2Error:
        pass

    done = threading.Event()
    box: dict[str, int] = {}

    class _ProbeHandler(_EnvCompletedHandler):
        def _invoke(self, this, hr, ptr):  # noqa: ANN001
            box["hr"] = hr
            done.set()
            return S_OK

    handler = _ProbeHandler(lambda addr: None)
    # 探测用的 user-data 目录必须自己找可写位置，**不能**直接依赖
    # ``tempfile.mkdtemp()``：TEMP 不可写时它会抛异常，导致所有 Runtime 版本
    # 都被误判为「不可用」，最终窗口空白（只剩背景色）。
    user_data = _probe_user_data_dir(folder.name if folder else "auto")
    if user_data is None:
        return False

    try:
        hr = lib.CreateCoreWebView2EnvironmentWithOptions(
            str(folder) if folder else None, str(user_data), None, _as_ptr(handler.pointer)
        )
        if hr < 0:
            return False
        # 在本线程泵消息等回调（与正式装配时的做法一致）
        deadline = time.monotonic() + timeout
        msg = _MSG()
        while not done.is_set():
            if time.monotonic() > deadline:
                return False
            while _user32.PeekMessageW(ctypes.byref(msg), None, 0, 0, PM_REMOVE):
                if msg.message == 0x0012:  # WM_QUIT
                    break
                _user32.TranslateMessage(ctypes.byref(msg))
                _user32.DispatchMessageW(ctypes.byref(msg))
            time.sleep(0.004)
        return box.get("hr", -1) >= 0
    except Exception:  # noqa: BLE001
        return False
    # 刻意**不删除**探测用的 user-data 目录（早期版本在这里 rmtree）。
    # 探测成功意味着 msedgewebview2.exe 已经在该目录上跑起来了，此时把目录
    # 抽掉会让那个浏览器进程直接崩溃/进入异常状态，并污染紧随其后的正式装配
    # ——表现为「探测说可用，真正建环境却报 0x80070002」。目录很小，留着无害。


def _probe_user_data_dir(name: str = "default") -> Path | None:
    """给探测用的 user-data 目录（按 Runtime 版本分目录，可复用）。

    顺序与 :func:`app.runtime_env.ensure_usable_temp_dir` 一致：
    环境变量 TEMP/TMP → 数据目录 → exe 同级 → 系统默认。
    任何一处能建目录就用它；全都不行才返回 None（此时 Runtime 探测无法进行）。

    ``name`` 用版本目录名：同一版本复用同一份探测 profile，既省一次冷启动，
    也避免每次探测都新建目录（用户目录被安全软件限制时会连建目录都失败）。
    """
    from app import runtime_env

    cands: list[Path] = []
    for key in ("TEMP", "TMP", "TMPDIR"):
        v = os.environ.get(key)
        if v:
            cands.append(Path(v))
    # 应用启动后 TEMP 已被改到数据目录下；直接用它最可靠
    try:
        data_dir = Path(runtime_env.resolve_data_dir(None))
        cands += [data_dir / "temp", data_dir / "webview2"]
    except Exception:  # noqa: BLE001
        pass
    try:
        cands.append(runtime_env.bundle_root() / "temp")
    except Exception:  # noqa: BLE001
        pass
    if os.name == "nt":
        pfx = os.environ.get("LOCALAPPDATA")
        if pfx:
            cands.append(Path(pfx) / "Temp")

    for base in cands:
        d = base / "wv2probe" / name
        try:
            d.mkdir(parents=True, exist_ok=True)
            return d
        except OSError:
            continue
    return None


_loader_lib: ctypes.WinDLL | None = None


def _load_loader() -> ctypes.WinDLL:
    global _loader_lib
    if _loader_lib is not None:
        return _loader_lib
    if os.name != "nt":
        raise Wv2Error("WebView2 原生窗口仅支持 Windows")
    path = find_loader_dll()
    if path is None:
        raise Wv2Error(
            "未找到 WebView2Loader.dll（发布包应把它放在 vendor/webview2 下）。"
        )
    try:
        lib = ctypes.WinDLL(str(path))
    except OSError as e:
        raise Wv2Error(f"加载 WebView2Loader.dll 失败（{path}）：{e}") from e

    lib.CreateCoreWebView2EnvironmentWithOptions.argtypes = [
        c_wchar_p, c_wchar_p, c_void_p, c_void_p,
    ]
    lib.CreateCoreWebView2EnvironmentWithOptions.restype = _HRESULT
    if hasattr(lib, "GetAvailableCoreWebView2BrowserVersionString"):
        lib.GetAvailableCoreWebView2BrowserVersionString.argtypes = [
            c_wchar_p, POINTER(c_wchar_p),
        ]
        lib.GetAvailableCoreWebView2BrowserVersionString.restype = _HRESULT
    _loader_lib = lib
    return lib


WEBVIEW2_DOWNLOAD_URL = "https://developer.microsoft.com/microsoft-edge/webview2/"


def webview2_runtime_version() -> str | None:
    """已安装的 WebView2 Runtime 版本；未安装返回 None。

    两条来源：

    1. 优先问 loader 的 ``GetAvailableCoreWebView2BrowserVersionString``；
    2. loader 问不出来时（典型：随包 loader 太旧、系统 Runtime 太新，会返回
       ``0x80070002``），退回 :func:`find_runtime_dir` 的目录名推断。

    第 2 条是必要的：否则「Runtime 明明装了」也会被判成未安装，
    进而导致窗口起不来、且自检给出误导性结论。
    """
    try:
        lib = _load_loader()
    except Wv2Error:
        lib = None
    if lib is not None and hasattr(lib, "GetAvailableCoreWebView2BrowserVersionString"):
        ver = c_wchar_p()
        hr = lib.GetAvailableCoreWebView2BrowserVersionString(None, ctypes.byref(ver))
        if hr >= 0 and ver.value:
            return ver.value
    # loader 探测失败：用目录名兜底
    rt = find_runtime_dir()
    if rt is not None:
        return Path(rt).name
    return None


def is_available() -> bool:
    """窗口能否起来：loader 可加载，且 WebView2 Runtime 可定位。"""
    if find_loader_dll() is None:
        return False
    return webview2_runtime_version() is not None or find_runtime_dir() is not None


def probe_environment(timeout: float = _PROBE_TIMEOUT) -> tuple[bool, str]:
    """真建一次 WebView2 环境，验证「窗口能不能起来」（自检用）。

    只检查 loader 与版本号是不够的：真机上出现过「版本号读得到、目录也在，
    但任何进程都建不出环境（``0x80070002``）」的情况——那类故障只有在真正
    创建环境时才暴露，而它恰好是「窗口白屏 / 起不来」的直接原因。
    """
    if os.name != "nt":
        return False, "原生窗口仅支持 Windows"
    try:
        _load_loader()
    except Wv2Error as e:
        return False, str(e)
    runtime_dir = find_runtime_dir()
    # 枚举不到目录时不能直接判死：按用户安装的运行时由 loader 自动发现即可。
    if _probe_runtime_dir(runtime_dir, timeout):
        return True, (
            f"Runtime {runtime_dir.name} 可创建环境"
            if runtime_dir is not None
            else "loader 自动发现可用"
        )
    version = webview2_runtime_version() or "版本未知"
    if runtime_dir is None and not _enumerate_runtime_dirs():
        return False, f"运行时可被 loader 发现（{version}），但创建环境失败"
    return False, (
        f"运行时可定位（{version}）但无法创建环境：多为安装不完整、位数不一致，"
        "或被安全软件拦截（需修复安装 / 装对应位数 / 加白名单）"
    )


def diagnose() -> list[str]:
    """返回关键诊断行（自检与错误提示共用，避免两处口径不一致）。"""
    loader = find_loader_dll()
    version = webview2_runtime_version()
    runtime_dir = find_runtime_dir()
    installed = _enumerate_runtime_dirs()
    lines = [
        f"WebView2Loader.dll：{loader or '未找到'}",
        f"WebView2 Runtime 版本：{version or '未检测到'}",
        f"可用 Runtime 目录：{runtime_dir or '未找到'}",
        f"本程序位数：{_HOST_ARCH}",
    ]
    # 目录在、但一个都不可用——这正是「装了却报没装」的现场，必须写出来
    if installed and runtime_dir is None:
        lines.append("发现安装目录但均无法使用：")
        for d in installed:
            arch = runtime_arch(d) or "位数未知"
            flag = "（位数与程序不一致，无法使用）" if arch not in ("位数未知", _HOST_ARCH) else ""
            lines.append(f"  - {d}（{arch}）{flag}")
    return lines


def window_failure_text(reason: str) -> str:
    """窗口起不来时给用户看的完整文字（原因 + 诊断 + 下一步）。

    为什么要有统一入口：窗口失败可能在 ``_run_inner``（宿主内）也可能在
    ``desktop._run_window``（启动器里）被捕获，两处各写一套文案必然漂移。
    """
    lines: list[str] = ["无法打开应用窗口（界面依赖 WebView2 渲染）。", "", reason, ""]
    try:
        diag = diagnose()
    except Exception:  # noqa: BLE001  诊断本身失败不应影响报错
        diag = []
    if diag:
        lines.append("诊断信息：")
        lines.extend(f"  {d}" for d in diag)
        lines.append("")
    lines.append(
        "处理办法（按顺序试）：\n"
        f"  1. 用官方 Evergreen 独立安装器修复 WebView2 Runtime：{WEBVIEW2_DOWNLOAD_URL}\n"
        "  2. 把 videorag.exe 加入安全软件（如腾讯电脑管家）的信任区/白名单后重试；\n"
        "  3. 只想先用起来：用 --headless 启动，再用浏览器打开日志里打印的地址。"
    )
    return "\n".join(lines)


def _environment_error_text(runtime_dir: Path | None, errors: list[str]) -> str:
    """把多次尝试的失败原因拼成用户能照着做的提示。

    关键是把「没装」与「装了但用不了」分开。真机上出现过「Runtime 已安装、版本号
    也读得到，但任何进程都建不出环境（0x80070002）」——若照旧提示「请安装
    WebView2 Runtime」，用户会反复重装，而问题其实在安装完整性或安全软件拦截上。
    """
    version = webview2_runtime_version()
    installed = _enumerate_runtime_dirs()
    lines: list[str] = ["无法初始化 WebView2（应用窗口依赖它渲染界面）。"]
    if version or runtime_dir is not None or installed:
        lines.append(f"已检测到 WebView2 Runtime：{version or '版本未知'}")
        if installed:
            lines.append("安装目录：")
            for d in installed:
                arch = runtime_arch(d) or "位数未知"
                lines.append(f"  - {d}（{arch}）")
        if runtime_dir is not None:
            lines.append(f"可用的运行时目录：{runtime_dir}")
        else:
            lines.append("可用目录：未能定位（正式装配会交给 loader 自动发现）")
        lines.append(
            "属于「已安装但无法创建环境」，常见原因：\n"
            "  1. 位数不一致——64 位程序用不了 32 位运行时（反之亦然）；\n"
            "  2. 运行时安装不完整——安装/升级被中断，目录在但文件缺失；\n"
            "  3. 安全软件（电脑管家 / 杀毒）拦截了本程序或 msedgewebview2.exe。"
        )
    else:
        lines.append("未检测到 WebView2 Runtime。")
    lines.append("尝试过的创建方式：")
    lines.extend(f"  - {e}" for e in errors)
    lines.append(
        "处理办法（按顺序试）：\n"
        f"  1. 用官方 Evergreen 独立安装器修复/重装：{WEBVIEW2_DOWNLOAD_URL}\n"
        "     （注意选与系统一致的位数：64 位系统装 x64 版）\n"
        "  2. 把本程序加入安全软件（如腾讯电脑管家）的信任区/白名单后重启本程序；\n"
        "  3. 仍然失败时，把以上几行连同日志文件一起反馈。"
    )
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# 窗口
# ---------------------------------------------------------------------------


class WebView2Window:
    """一个 Win32 顶层窗口 + 内嵌 WebView2 视图。"""

    def __init__(
        self,
        title: str = "videoRAG",
        width: int = 1360,
        height: int = 900,
        min_size: tuple[int, int] = (1000, 640),
        background: tuple[int, int, int] = (0x14, 0x17, 0x1C),
        icon: Path | str | None = None,
        storage_dir: Path | str | None = None,
    ) -> None:
        self.title = title
        self.width = width
        self.height = height
        self.min_size = min_size
        self.background = background
        self.icon_path = Path(icon) if icon else None
        self.storage_dir = Path(storage_dir) if storage_dir else None

        self.hwnd: int | None = None
        self.env: c_void_p | None = None
        self.controller: c_void_p | None = None
        self.webview: c_void_p | None = None

        self._wndproc_cb = None      # 必须保活
        self._handlers: list[_ComHandler] = []   # 必须保活
        self._init_error: str | None = None
        self._pending_url: str | None = None
        self._controller_ready = False
        self._navigated = False
        self._ready_evt = threading.Event()

    # ---- 窗口过程 ----
    def _wndproc(self, hwnd, msg, wparam, lparam):
        try:
            if msg == WM_SIZE:
                self._fit_controller()
                return 0
            if msg == WM_GETMINMAXINFO:
                mmi = ctypes.cast(lparam, POINTER(_MINMAXINFO)).contents
                mmi.ptMinTrackSize_x = self.min_size[0]
                mmi.ptMinTrackSize_y = self.min_size[1]
                return 0
            if msg == WM_CLOSE:
                _user32.DestroyWindow(hwnd)
                return 0
            if msg == WM_DESTROY:
                _user32.PostQuitMessage(0)
                return 0
        except Exception:  # noqa: BLE001  窗口过程内不能抛
            log.exception("窗口过程异常")
        return _user32.DefWindowProcW(hwnd, msg, wparam, lparam)

    # ---- 建窗 ----
    def _register_class(self) -> str:
        global _class_registered
        if _class_registered:
            return _WINDOW_CLASS

        self._wndproc_cb = WNDPROC(self._wndproc)
        hinst = _kernel32.GetModuleHandleW(None)

        IMAGE_ICON, LR_LOADFROMFILE = 1, 0x0010
        hicon = hicon_sm = None
        if self.icon_path and self.icon_path.is_file():
            hicon = _user32.LoadImageW(
                None, str(self.icon_path), IMAGE_ICON,
                _user32.GetSystemMetrics(SM_CXICON), _user32.GetSystemMetrics(SM_CYICON),
                LR_LOADFROMFILE,
            )
            hicon_sm = _user32.LoadImageW(
                None, str(self.icon_path), IMAGE_ICON,
                _user32.GetSystemMetrics(SM_CXSMICON), _user32.GetSystemMetrics(SM_CYSMICON),
                LR_LOADFROMFILE,
            )

        r, g, b = self.background
        wc = WNDCLASSEXW()
        wc.cbSize = ctypes.sizeof(WNDCLASSEXW)
        wc.style = 0
        wc.lpfnWndProc = self._wndproc_cb
        wc.hInstance = hinst
        wc.hIcon = hicon
        wc.hIconSm = hicon_sm
        wc.hCursor = _user32.LoadCursorW(None, ctypes.c_void_p(IDC_ARROW))
        wc.hbrBackground = _gdi32.CreateSolidBrush((b << 16) | (g << 8) | r)
        wc.lpszClassName = _WINDOW_CLASS

        if not _user32.RegisterClassExW(ctypes.byref(wc)):
            err = ctypes.get_last_error()
            if err not in (0, 1410):  # 1410 = ERROR_CLASS_ALREADY_EXISTS
                raise Wv2Error(f"RegisterClassExW 失败（GetLastError={err}）")
        _class_registered = True
        return _WINDOW_CLASS

    def create(self) -> None:
        """创建顶层窗口（此时不显示）。"""
        cls = self._register_class()
        hinst = _kernel32.GetModuleHandleW(None)

        # 按目标客户区尺寸反推外框
        rect = _RECT(0, 0, self.width, self.height)
        _user32.AdjustWindowRectEx(ctypes.byref(rect), WS_OVERLAPPEDWINDOW, False, 0)
        w, h = rect.right - rect.left, rect.bottom - rect.top

        hwnd = _user32.CreateWindowExW(
            0, cls, self.title, WS_OVERLAPPEDWINDOW | WS_CLIPCHILDREN,
            CW_USEDEFAULT, CW_USEDEFAULT, w, h,
            None, None, hinst, None,
        )
        if not hwnd:
            raise Wv2Error(f"CreateWindowExW 失败（GetLastError={ctypes.get_last_error()}）")
        self.hwnd = hwnd

    # ---- 消息泵 ----
    def _pump_until(self, ev: threading.Event, timeout: float) -> bool:
        """在**不进入阻塞式消息循环**的前提下泵消息，直到 ev 置位或超时。

        WebView2 的异步回调依赖宿主线程泵消息；如果像普通线程那样
        ``Event.wait()``，回调永远送不到，必然超时失败。
        """
        deadline = time.monotonic() + timeout
        msg = _MSG()
        while not ev.is_set():
            if time.monotonic() > deadline:
                return False
            while _user32.PeekMessageW(ctypes.byref(msg), None, 0, 0, PM_REMOVE):
                if msg.message == 0x0012:  # WM_QUIT
                    return ev.is_set()
                _user32.TranslateMessage(ctypes.byref(msg))
                _user32.DispatchMessageW(ctypes.byref(msg))
            time.sleep(0.004)
        return True

    # ---- WebView2 装配 ----
    def _fit_controller(self) -> None:
        if not (self.hwnd and self.controller):
            return
        rect = _RECT()
        if not _user32.GetClientRect(self.hwnd, ctypes.byref(rect)):
            return
        try:
            # ICoreWebView2Controller [6] set_Bounds(RECT)
            set_bounds = _vtbl_method(self.controller, 6, _HRESULT, POINTER(_RECT))
            set_bounds(_as_ptr(self.controller), ctypes.byref(rect))
        except Wv2Error:
            pass

    def _create_environment(self) -> c_void_p:
        """创建 WebView2 环境，并在**同一个回调栈内**完成控制器与视图装配。

        这是整个模块最关键的一条约束，踩过很多坑才确定：

        * WebView2 的环境对象带**线程/公寓亲和性**，而且绑定的是「投递回调的那段
          调用栈」。一旦 env 的 CompletedHandler 返回，再调用 ``env`` 上的任何方法
          （包括 ``CreateCoreWebView2Controller``）都会得到
          ``ERROR_WRONG_THREAD (0x802A000C)``——即使看起来是同一个 Python 线程。
        * 同理，**不能在回调里再嵌套一个消息泵**（``_pump``）去等 controller：
          那会让回调提前返回，controller 创建随之失效。正确做法是在 env 回调里
          **同步**发起 ``CreateCoreWebView2Controller``，让它的回调继续在同一栈上被
          派发——所以这里的等待必须由**最外层**的 ``_pump_until`` 统一完成。
        """
        from app import runtime_env

        lib = _load_loader()
        user_data = (self.storage_dir or Path(runtime_env.resolve_data_dir(None))) / "webview2"
        user_data.mkdir(parents=True, exist_ok=True)

        # 两种失败方式在真机上都出现过，而且原因相反，所以两条路必须互为兜底：
        # * loader 较旧 + 系统 Runtime 较新 → **自动发现**返回 0x80070002（「文件未
        #   找到」，看着像没装），必须显式给目录；
        # * 显式目录被杀软/管家类软件拦截、或该目录半损坏 → 显式失败，交给**自动
        #   发现**反而能起来。
        runtime_dir = find_runtime_dir()
        attempts: list[str | None] = []
        if runtime_dir is not None:
            attempts.append(str(runtime_dir))
        attempts.append(None)  # None = 交给 WebView2Loader 自动发现

        errors: list[str] = []
        # 两次尝试共用一个时间预算：否则「回调迟迟不来」时最坏要等 2×超时，
        # 用户这段时间只看到一个深色空窗，和卡死没有区别。
        deadline = time.monotonic() + _HANDLER_TIMEOUT
        for folder in attempts:
            remaining = deadline - time.monotonic()
            if remaining <= 0.5:
                errors.append(f"{folder or '(自动发现)'} → 预算已用尽，未再尝试")
                break
            log.info(
                "创建 WebView2 环境：%s",
                f"指定 Runtime 目录 {folder}" if folder else "交给 loader 自动发现",
            )
            try:
                return self._create_environment_once(lib, folder, user_data, remaining)
            except Wv2Error as e:
                errors.append(f"{folder or '(自动发现)'} → {e}")
                if len(attempts) > 1:
                    log.warning("该方式创建 WebView2 环境失败，尝试下一种：%s", e)
        raise Wv2Error(_environment_error_text(runtime_dir, errors))

    def _create_environment_once(
        self, lib, browser_folder: str | None, user_data: Path, timeout: float = _HANDLER_TIMEOUT
    ) -> c_void_p:
        """用指定的 ``browser_folder`` 创建环境并完成控制器装配（单次尝试）。"""
        self._env_error: str | None = None
        self._ready_evt = threading.Event()

        def on_env(env_addr: int) -> None:
            """env 回调：立刻在同一栈里建 controller。"""
            try:
                # 回调给的 env 是借用引用，存下来前必须自己持有一份
                self.env = c_void_p(env_addr)
                _addref(self.env)
                ctrl_handler = _CtrlCompletedHandler(self._on_ctrl_ready)
                self._handlers.append(ctrl_handler)
                # ICoreWebView2Environment [3] CreateCoreWebView2Controller(HWND, handler)
                create = _vtbl_method(self.env, 3, _HRESULT, c_void_p, c_void_p)
                hr = create(_as_ptr(self.env), self.hwnd, _as_ptr(ctrl_handler.pointer))
                if hr < 0:
                    self._env_error = (
                        f"创建 WebView2 控制器失败（0x{hr & 0xFFFFFFFF:08X}）"
                    )
                    self._ready_evt.set()
            except Exception as e:  # noqa: BLE001
                self._env_error = f"{type(e).__name__}: {e}"
                log.exception("WebView2 环境回调异常")
                self._ready_evt.set()

        env_handler = _EnvCompletedHandler(on_env)
        self._handlers.append(env_handler)

        hr = lib.CreateCoreWebView2EnvironmentWithOptions(
            browser_folder, str(user_data), None, _as_ptr(env_handler.pointer)
        )
        if hr < 0:
            raise Wv2Error(f"CreateCoreWebView2EnvironmentWithOptions 返回 0x{hr & 0xFFFFFFFF:08X}")
        # 统一在这里泵消息：env → controller → 导航 全部在这一个循环里被派发
        if not self._pump_until(self._ready_evt, timeout):
            raise Wv2Error(f"等待 WebView2 就绪超时（{timeout:.0f}s，回调未到达）")
        if env_handler.error:
            raise Wv2Error(env_handler.error)
        if self._env_error:
            raise Wv2Error(self._env_error)
        return self.env

    def _on_ctrl_ready(self, addr: int) -> None:
        """controller 回调：在同一回调栈里完成视图初始化与导航。

        **导航必须在这里做。** 实测：controller 回调返回之后再调用 ``Navigate``
        （无论是紧接着的代码、还是由窗口消息派发）都会得到
        ``ERROR_INVALID_STATE (0x8007139F)``——WebView2 只在 controller 创建回调
        的同步调用栈内接受导航。放在回调里调用则返回 S_OK，页面正常加载。
        """
        try:
            # 同上：controller 也是借用引用
            self.controller = c_void_p(addr)
            _addref(self.controller)
            self._setup_controller()
            if self._pending_url:
                self._navigate_now(self._pending_url)
            self._controller_ready = True
        except Exception as e:  # noqa: BLE001
            self._env_error = f"{type(e).__name__}: {e}"
            log.exception("WebView2 控制器初始化失败")
        finally:
            self._ready_evt.set()

    def _setup_controller(self) -> None:
        """控制器就绪后的设置（必须在回调栈内调用）。"""
        # ICoreWebView2Controller [4] put_IsVisible(BOOL)
        set_visible = _vtbl_method(self.controller, 4, _HRESULT, c_int)
        set_visible(_as_ptr(self.controller), 1)
        self._fit_controller()

        # ICoreWebView2Controller [25] get_CoreWebView2(ICoreWebView2**)
        get_core = _vtbl_method(self.controller, 25, _HRESULT, POINTER(c_void_p))
        core = c_void_p()
        hr = get_core(_as_ptr(self.controller), ctypes.byref(core))
        if hr < 0 or not core:
            raise Wv2Error(f"获取 CoreWebView2 失败（0x{hr & 0xFFFFFFFF:08X}）")
        self.webview = core

        self._set_background_color()
        self._enable_default_context_menus(False)
        self._enable_devtools(not _frozen())

    def _set_background_color(self) -> None:
        """``ICoreWebView2Controller2::put_DefaultBackgroundColor``（可选接口）。

        深色底避免加载瞬间白闪。该属性不在基础接口上，需 QueryInterface 探测；
        拿不到就静默跳过，不影响功能。

        下标注意：``ICoreWebView2Controller2`` 是**独立接口**，vtable 从
        ``[3] get_DefaultBackgroundColor`` / ``[4] put_DefaultBackgroundColor``
        开始——**不是**接在 ``ICoreWebView2Controller`` 后面（那样会算成 26/27，
        调用到别的方法上）。早期写成 27 是错的。
        """
        ctrl2 = _qi(self.controller, GUID("C979903E-D4CA-4524-92EB-347BFEB95BA3"))
        if ctrl2 is None:
            return
        try:
            # [4] put_DefaultBackgroundColor(COREWEBVIEW2_COLOR{B,G,R,A})
            put = _vtbl_method(ctrl2, 4, _HRESULT, c_uint)
            r, g, b = self.background
            put(_as_ptr(ctrl2), (0xFF << 24) | (r << 16) | (g << 8) | b)
        finally:
            _release(ctrl2)

    def _enable_default_context_menus(self, enabled: bool) -> None:
        """关闭 WebView2 默认右键菜单，让产品界面自己的菜单生效。

        下标 **[14]** = ``ICoreWebView2Settings::put_AreDefaultContextMenusEnabled``。
        早期误写成 4，而 4 是 ``put_IsScriptEnabled``——等于把页面的 JavaScript
        整个关掉，后果是 WebView 显示空白、且后续 ``Navigate`` 报
        ``ERROR_INVALID_STATE (0x8007139F)``，极难定位。
        """
        settings = c_void_p()
        try:
            # ICoreWebView2 [3] get_Settings(ICoreWebView2Settings**)
            get_settings = _vtbl_method(self.webview, 3, _HRESULT, POINTER(c_void_p))
            if get_settings(_as_ptr(self.webview), ctypes.byref(settings)) < 0 or not settings:
                return
            # ICoreWebView2Settings [14] put_AreDefaultContextMenusEnabled(BOOL)
            put = _vtbl_method(settings, 14, _HRESULT, c_int)
            put(_as_ptr(settings), 1 if enabled else 0)
        except Wv2Error:
            pass
        finally:
            if settings:
                _release(settings)

    def _enable_devtools(self, enabled: bool) -> None:
        """开关开发者工具。

        下标 **[12]** = ``ICoreWebView2Settings::put_AreDevToolsEnabled``。
        早期误写成 6（= ``put_AreWebMessageEnabled``），会切断宿主与页面的
        ``postMessage`` 通道。
        """
        settings = c_void_p()
        try:
            get_settings = _vtbl_method(self.webview, 3, _HRESULT, POINTER(c_void_p))
            if get_settings(_as_ptr(self.webview), ctypes.byref(settings)) < 0 or not settings:
                return
            # ICoreWebView2Settings [12] put_AreDevToolsEnabled(BOOL)
            put = _vtbl_method(settings, 12, _HRESULT, c_int)
            put(_as_ptr(settings), 1 if enabled else 0)
        except Wv2Error:
            pass
        finally:
            if settings:
                _release(settings)

    def navigate(self, url: str) -> None:
        """导航到指定地址。

        导航同样有线程亲和性，必须回到回调线程执行：这里把请求记下来，
        由 ``_setup_controller_in_callback`` 在回调里消费。若装配已完成，
        则用 ``_pending_url`` 交给回调线程的机会已经错过，直接尝试调用
        （若返回 WRONG_THREAD 会抛出可读错误）。
        """
        self._pending_url = url
        if self.webview and self._controller_ready:
            self._navigate_now(url)

    def _navigate_now(self, url: str) -> None:
        """真正执行导航（只能在回调线程内调用）。"""
        if not self.webview:
            raise Wv2Error("WebView 尚未就绪")
        # ICoreWebView2 [5] Navigate(LPCWSTR)
        navigate = _vtbl_method(self.webview, 5, _HRESULT, c_wchar_p)
        hr = navigate(_as_ptr(self.webview), url)
        if hr < 0:
            raise Wv2Error(f"导航失败（0x{hr & 0xFFFFFFFF:08X}）：{url}")
        self._navigated = True

    # ---- 主流程 ----
    def run(self, url: str) -> int:
        """建窗 → 装配 WebView2 → 导航 → 消息循环直到关闭。"""
        _co_initialize()
        try:
            return self._run_inner(url)
        finally:
            self._teardown()
            _co_uninitialize()

    def _run_inner(self, url: str) -> int:
        self.create()
        _user32.ShowWindow(self.hwnd, SW_SHOWNORMAL)
        _user32.UpdateWindow(self.hwnd)

        # 记录待导航地址；装配完成后由消息循环派发（不能放在回调栈内，见 _on_ctrl_ready）
        self._pending_url = url

        try:
            self._create_environment()   # 只做装配，不导航
            _user32.SetWindowTextW(self.hwnd, self.title)
        except Exception as e:  # noqa: BLE001
            self._init_error = f"{type(e).__name__}: {e}"
            # 弹窗内容含诊断与处置步骤：窗口形态没有控制台，日志在受限环境里也
            # 可能写不出来，这是用户唯一能看到的线索。日志里同样留一份全文，
            # 便于事后汇总（弹窗内容不会被任何地方记录）。
            failure = window_failure_text(str(e))
            log.error("WebView2 初始化失败：\n%s", failure)
            log.debug("原始异常", exc_info=True)
            try:
                _user32.MessageBoxW(None, failure, "videoRAG 窗口初始化失败", 0x10)
            except Exception:  # noqa: BLE001
                pass
            _user32.DestroyWindow(self.hwnd)

        # 装配（含导航）已在 controller 回调栈内完成，这里只跑消息循环等窗口关闭。
        # 注意：不能把导航推迟到这里——回调之外调用 Navigate 会报
        # ERROR_INVALID_STATE (0x8007139F)，见 _on_ctrl_ready 的说明。
        msg = _MSG()
        while True:
            ret = _user32.GetMessageW(ctypes.byref(msg), None, 0, 0)
            if ret <= 0:  # 0 = WM_QUIT，-1 = 错误
                break
            _user32.TranslateMessage(ctypes.byref(msg))
            _user32.DispatchMessageW(ctypes.byref(msg))

        return 0 if self._init_error is None else 1

    def _teardown(self) -> None:
        """关闭控制器并释放引用。

        这里的顺序和参数形式都很讲究，踩过坑：

        * ``WM_DESTROY`` 之后，WebView2 已经随宿主窗口销毁回收了 controller /
          webview 的内部对象；此时**再去读它的 vtable 就是访问冲突**
          （实测 ``0xc0000005``，崩溃模块 ``_ctypes.pyd``，栈在 ``_vtbl_method``）。
          所以窗口一关，这里只放弃引用，**不再调用任何 COM 方法**；
        * 需要显式关闭时走 :meth:`close_controller`，它必须发生在窗口销毁之前；
        * 所有 ``this`` 实参必须传 **int 地址**（``_as_ptr``），传 ``c_void_p``
          容器会让 ctypes 传容器的地址而非指针值，同样导致崩溃。
        """
        self.webview = None
        self.controller = None
        self.env = None

    def close_controller(self) -> None:
        """在窗口销毁**之前**主动关闭控制器（可选，正常退出不必须）。

        调用 ``ICoreWebView2Controller::Close()`` 会同步拆掉 WebView 及其子对象；
        之后这些指针立即失效，因此本方法把状态一并清空，避免后续误用。
        """
        if self.controller:
            try:
                # ICoreWebView2Controller [24] Close()
                close = _vtbl_method(self.controller, 24, _HRESULT)
                close(_as_ptr(self.controller))
            except Exception:  # noqa: BLE001
                pass
        self.webview = None
        self.controller = None
        if self.env:
            _release(self.env)
        self.env = None


def _addref(ptr) -> None:
    """调用 IUnknown::AddRef（vtable[1]），失败静默。

    **为什么必须显式做**：异步创建 API 回调里传进来的 ``environment`` /
    ``controller`` 都是**借用引用**（borrowed reference），所有权仍在调用方，
    回调一返回运行时就会 Release。若把地址直接存下来长期使用，回调返回后
    对象即被销毁：浏览器进程干净退出（没有崩溃记录），我们手里的指针变成
    悬空指针——后续 ``Navigate`` 可能仍返回 S_OK，但页面永远停在 about:blank，
    窗口只有一片背景色。这就是「灰屏无内容」的真实成因。
    """
    addr = _as_ptr(ptr)
    if not addr:
        return
    try:
        fn = _vtbl_method(addr, 1, _ULONG)
        fn(addr)
    except Exception:  # noqa: BLE001
        pass


def _release(ptr) -> None:
    """调用 IUnknown::Release（vtable[2]）。失败静默。"""
    addr = _as_ptr(ptr)
    if not addr:
        return
    try:
        fn = _vtbl_method(addr, 2, _ULONG)
        fn(addr)
    except Exception:  # noqa: BLE001
        pass


def _frozen() -> bool:
    return bool(getattr(sys, "frozen", False))


def run_window(
    url: str,
    *,
    title: str = "videoRAG",
    width: int = 1360,
    height: int = 900,
    min_size: tuple[int, int] = (1000, 640),
    background: tuple[int, int, int] = (0x14, 0x17, 0x1C),
    icon: Path | str | None = None,
    storage_dir: Path | str | None = None,
) -> int:
    """在主线程打开 WebView2 原生窗口，阻塞到窗口关闭。

    必须在主线程调用：Win32 消息循环有线程亲和性。uvicorn 跑在后台线程，
    两者互不干扰。
    """
    if os.name != "nt":
        raise Wv2Error("WebView2 原生窗口仅支持 Windows")
    win = WebView2Window(
        title=title,
        width=width,
        height=height,
        min_size=min_size,
        background=background,
        icon=icon,
        storage_dir=storage_dir,
    )
    return win.run(url)
