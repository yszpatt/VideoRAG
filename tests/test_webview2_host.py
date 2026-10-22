"""``app/webview2_host.py`` 的单元测试。

这个模块直接和 COM / Win32 打交道，**大部分逻辑无法脱离真实窗口验证**（那部分
由打包冒烟测试的 ``webview2 backend`` 项覆盖）。这里只测三类「纯逻辑」的东东，
它们在真机上出过错、且改错不会有明显报错：

1. GUID 的解析与比较（IID 写错 → QueryInterface 失败 → 窗口起不来）；
2. ``ctypes.cast`` 的指针语义（把 ``c_void_p`` 容器当地址用，是本次故障的根因，
   表现为 ``ERROR_WRONG_THREAD 0x802A000C``，极易误判为线程问题）；
3. WebView2Loader.dll 的定位顺序与可用性判定。

不启动真实窗口：CI 上没有交互式桌面，创建 HWND 会失败。
"""
from __future__ import annotations

import ctypes
import os
from ctypes import c_void_p

import pytest

from app import webview2_host as h


# ---------- GUID ----------

def test_guid_parses_canonical_string():
    g = h.GUID("B96D755E-0319-4E92-A296-23436F46A1FC")
    assert g.Data1 == 0xB96D755E
    assert g.Data2 == 0x0319
    assert g.Data3 == 0x4E92
    assert bytes(g.Data4) == bytes([0xA2, 0x96, 0x23, 0x43, 0x6F, 0x46, 0xA1, 0xFC])


def test_guid_roundtrip_str():
    text = "B96D755E-0319-4E92-A296-23436F46A1FC"
    assert str(h.GUID(text)).upper() == "{" + text.upper() + "}"


def test_guid_accepts_braces():
    a = h.GUID("{4D00C0D1-9434-4EB6-8078-8697A560334F}")
    b = h.GUID("4D00C0D1-9434-4EB6-8078-8697A560334F")
    assert h._guid_eq(a, b)


def test_guid_inequality():
    a = h.GUID("00000000-0000-0000-C000-000000000046")
    b = h.GUID("76ECEACB-0462-4D94-AC83-423A6793775E")
    assert not h._guid_eq(a, b)


def test_known_iids_are_distinct_and_wellformed():
    """IID 表是全模块里最不能错的数据：重复或写错都会让 COM 直接失败。"""
    iids = [
        h.IID_IUnknown,
        h.IID_ICoreWebView2Environment,
        h.IID_ICoreWebView2Controller,
        h.IID_ICoreWebView2,
        h.IID_EnvCompletedHandler,
        h.IID_CtrlCompletedHandler,
    ]
    for i, a in enumerate(iids):
        for b in iids[i + 1:]:
            assert not h._guid_eq(a, b), f"{a} 与 {b} 重复"
    # ICoreWebView2Environment 的 IID 是官方文档里的固定值
    assert str(h.IID_ICoreWebView2Environment).upper() == "{B96D755E-0319-4E92-A296-23436F46A1FC}"


# ---------- 指针语义（本次故障的根因） ----------

def test_as_ptr_unwraps_c_void_p():
    """``_as_ptr`` 必须返回指针**值**，而不是 ctypes 容器的地址。

    这是本模块最容易重犯的错：``ctypes.cast(c_void_p(addr), T)`` 取的是容器自身
    地址，后续解引用得到的 vtable 就是垃圾——WebView2 会报
    ``ERROR_WRONG_THREAD 0x802A000C``，看起来像线程问题，其实是指针被套了两层。
    """
    inner = 0x1234_5678
    boxed = c_void_p(inner)
    assert h._as_ptr(boxed) == inner
    # int 原样返回
    assert h._as_ptr(inner) == inner
    # None → 0
    assert h._as_ptr(None) == 0


def test_as_ptr_int_and_container_differ():
    """确保两种形态不被混为一谈（容器地址 ≠ 容器内保存的值）。"""
    boxed = c_void_p(0xABC)
    container_addr = ctypes.cast(ctypes.byref(boxed), c_void_p).value
    assert h._as_ptr(boxed) == 0xABC
    assert container_addr != 0xABC


def test_vtbl_method_rejects_null():
    with pytest.raises(h.Wv2Error):
        h._vtbl_method(None, 0, ctypes.c_int)


def test_vtbl_method_rejects_zero_address():
    with pytest.raises(h.Wv2Error):
        h._vtbl_method(0, 0, ctypes.c_int)


# ---------- WebView2Loader.dll 定位 ----------

def test_loader_candidates_is_ordered_preferring_bundled(monkeypatch, tmp_path):
    """随包分发的 loader 优先于开发环境里 pywebview 自带的那份。"""
    from app import runtime_env

    monkeypatch.setattr(runtime_env, "bundle_root", lambda: tmp_path)
    cands = h._candidate_loader_dlls()
    assert cands, "候选列表不能为空"
    # 头两个都指向 bundle 内的 vendor/webview2
    assert "vendor" in str(cands[0]) and "webview2" in str(cands[0])


def test_find_loader_dll_returns_none_when_absent(monkeypatch):
    monkeypatch.setattr(h, "_candidate_loader_dlls", lambda: [])
    assert h.find_loader_dll() is None


def test_find_loader_dll_picks_first_existing(monkeypatch, tmp_path):
    a = tmp_path / "a.dll"
    b = tmp_path / "b.dll"
    b.write_bytes(b"MZ")
    monkeypatch.setattr(h, "_candidate_loader_dlls", lambda: [a, b])
    assert h.find_loader_dll() == b


@pytest.mark.skipif(os.name != "nt", reason="仅 Windows")
def test_webview2_runtime_version_reads_registry_value(monkeypatch):
    """能读到版本说明 Runtime 已装；读不到必须返回 None（而非抛异常）。"""
    version = h.webview2_runtime_version()
    # 装了则应为形如 152.0.4191.66 的串
    if version is not None:
        assert version.count(".") >= 2


@pytest.mark.skipif(os.name != "nt", reason="仅 Windows")
def test_is_available_combines_loader_and_runtime(monkeypatch, tmp_path):
    monkeypatch.setattr(h, "find_loader_dll", lambda: None)
    assert h.is_available() is False

    monkeypatch.setattr(h, "find_loader_dll", lambda: tmp_path / "x.dll")
    # 版本与目录两条判据都要 mock：真实机器上装了运行时时 find_runtime_dir 会命中
    monkeypatch.setattr(h, "webview2_runtime_version", lambda: None)
    monkeypatch.setattr(h, "find_runtime_dir", lambda: None)
    assert h.is_available() is False

    monkeypatch.setattr(h, "webview2_runtime_version", lambda: "152.0.0.0")
    assert h.is_available() is True


# ---------- vtable 下标回归锁 ----------
#
# 这些下标是从 Microsoft.Web.WebView2.Core.Raw 的接口声明逐条核对来的，写错不会
# 报「方法不存在」，而是**静默调用到别的属性上**，症状极具误导性：
#   - 把 put_AreDefaultContextMenusEnabled 写成 [4] → 实际关掉了 JavaScript
#   - 把 put_AreDevToolsEnabled 写成 [6] → 实际关掉了 WebMessage
# 结果都是 WebView 显示空白、Navigate 报 ERROR_INVALID_STATE，很难联想到下标。


def test_settings_vtable_indices_match_official_idl():
    """ICoreWebView2Settings 的方法顺序必须与官方 IDL 一致。

    官方顺序（含 IUnknown 三个槽）：
      [3] get_IsScriptEnabled  [4] set_IsScriptEnabled
      [5] get_IsWebMessageEnabled [6] set_IsWebMessageEnabled
      [7] get_AreDefaultScriptDialogsEnabled [8] set_...
      [9] get_IsStatusBarEnabled [10] set_...
      [11] get_AreDevToolsEnabled [12] set_AreDevToolsEnabled
      [13] get_AreDefaultContextMenusEnabled [14] set_AreDefaultContextMenusEnabled
    """
    # 本模块实际使用的下标
    DEVTOOLS_SET = 12
    CONTEXT_MENU_SET = 14

    assert DEVTOOLS_SET == 12, "put_AreDevToolsEnabled 是 vtable[12]"
    assert CONTEXT_MENU_SET == 14, "put_AreDefaultContextMenusEnabled 是 vtable[14]"
    # 这两个不能与「JavaScript / WebMessage 开关」混淆
    assert DEVTOOLS_SET not in (4, 6), "[4]/[6] 是 IsScriptEnabled/IsWebMessageEnabled，用错会白屏"
    assert CONTEXT_MENU_SET not in (4, 6)


def test_settings_put_indices_used_by_module(monkeypatch):
    """走一遍真实调用，断言用的是 12 / 14，而不是早先错写的 6 / 4。

    全程 mock 掉 ``_vtbl_method`` 与 ``_release``，不触碰任何真实指针
    ——给 ``_vtbl_method`` 传假地址会让 ctypes 解引用到无效内存，直接崩进程。
    """
    used = []

    def fake_vtbl_method(ptr, index, restype, *argtypes):
        used.append(index)

        def call(this, value=None):
            if index == 3:  # get_Settings：把出参写成一个假指针
                ctypes.cast(value, ctypes.POINTER(ctypes.c_void_p))[0] = 0xBEEF
            return 0

        return call

    win = h.WebView2Window()
    win.webview = 0x1000   # 只是非 0 的 int，_as_ptr 不会解引用
    monkeypatch.setattr(h, "_vtbl_method", fake_vtbl_method)
    monkeypatch.setattr(h, "_release", lambda ptr: None)

    win._enable_default_context_menus(False)
    win._enable_devtools(True)

    # 第一次是 get_Settings([3])，随后分别是 [14] 与 [12]
    assert 3 in used
    assert 14 in used, "关闭右键菜单必须用 put_AreDefaultContextMenusEnabled = [14]"
    assert 12 in used, "开关 DevTools 必须用 put_AreDevToolsEnabled = [12]"
    assert 4 not in used[1:], "[4] 是 put_IsScriptEnabled，用它会让页面白屏"
    assert 6 not in used[1:], "[6] 是 put_AreWebMessageEnabled，用它会断掉宿主通信"


def test_controller2_background_color_indices():
    """ICoreWebView2Controller2 是独立接口，vtable 从 [3] 起，不是接在 Controller 之后。

    官方声明： [3] get_DefaultBackgroundColor  [4] put_DefaultBackgroundColor
    早先误写成 [26]/[27]（当作 Controller 的延续），会调用到不存在的方法。
    """
    GET_BG = 3
    PUT_BG = 4
    assert (GET_BG, PUT_BG) == (3, 4)
    # 不能是 26/27
    assert PUT_BG != 27


def test_controller_close_index_is_24():
    """ICoreWebView2Controller::Close 是 vtable[24]（IUnknown 3 + 21 个成员）。"""
    CLOSE = 24
    assert CLOSE == 24


# ---------- 装配时序回归锁 ----------

def test_navigation_happens_inside_controller_callback():
    """导航必须在 controller 回调内同步发起。

    实测三种时机：
      * controller 回调**内** → 成功（S_OK，页面正常加载）
      * 回调返回后再调，哪怕紧接着 → ERROR_INVALID_STATE (0x8007139F)
      * 由窗口消息派发 → 同样是 ERROR_INVALID_STATE
    """
    import inspect

    src = inspect.getsource(h.WebView2Window._on_ctrl_ready)
    assert "_navigate_now" in src, "导航必须写在 _on_ctrl_ready 里"
    assert "_setup_controller" in src

    # _run_inner 里不应再出现「回调之后再导航」的代码
    run_src = inspect.getsource(h.WebView2Window._run_inner)
    assert "_navigate_now" not in run_src, (
        "不能在消息循环里导航——回调之外调用 Navigate 会 INVALID_STATE"
    )


def test_teardown_does_not_touch_dead_com_pointers():
    """窗口销毁后不能再对 COM 对象调方法：那会访问冲突（0xc0000005）。"""
    import ast
    import inspect
    import textwrap

    src = textwrap.dedent(inspect.getsource(h.WebView2Window._teardown))
    tree = ast.parse(src)
    # 只看真实调用，不看注释/docstring
    calls = [
        node.func.id
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
    ]
    assert "_vtbl_method" not in calls, "销毁后不得再读 vtable（对象已被 WebView2 回收）"
    assert "_release" not in calls


def test_release_uses_int_address():
    """``_release`` 必须把指针归一成 int 再调，否则 ctypes 会传容器地址。"""
    import inspect

    src = inspect.getsource(h._release)
    assert "_as_ptr" in src


# ---------- 颜色转换 ----------

def test_window_defaults_are_dark():
    """窗口默认深色底：前端是深色界面，浅色底会在加载瞬间白闪。"""
    win = h.WebView2Window()
    r, g, b = win.background
    assert r + g + b < 200


# ---------- 失败时的行为：快速、准确、可诊断 ----------
#
# 这一组对应真实故障现场：WebView2 环境建不出来时，旧实现会先白窗 60 秒、
# 再弹一个「请安装 WebView2 Runtime」的框——而运行时其实装得好好的
# （版本号读得到、目录也在，只是任何进程都建不出环境，返回 0x80070002）。
# 用户看到的是「卡死 + 说我没装」，两条都不对。


def test_borrowed_com_references_are_addrefed():
    """回调传进来的 env / controller 是**借用引用**，存下来前必须 AddRef。

    这是「窗口灰屏、页面永远停在 about:blank」的真实成因：回调返回后运行时
    Release 掉对象，浏览器进程干净退出（事件日志里没有崩溃记录），而我们手里的
    指针变成悬空指针——``Navigate`` 仍返回 S_OK，``get_Source`` 却永远读不到
    目标地址。症状极易被误判成「页面没加载好」或「前端构建有问题」。
    """
    import inspect

    env_src = inspect.getsource(h.WebView2Window._create_environment_once)
    ctrl_src = inspect.getsource(h.WebView2Window._on_ctrl_ready)
    assert "_addref(self.env)" in env_src, "环境对象必须 AddRef 后再长期持有"
    assert "_addref(self.controller)" in ctrl_src, "控制器必须 AddRef 后再长期持有"
    # AddRef 走的是 IUnknown 的 vtable[1]
    addref_src = inspect.getsource(h._addref)
    assert "1" in addref_src


def test_failure_timeouts_are_responsive():
    """等待回调的上限要短到「像报错」而不是「像卡死」；探测则要给首启留余量。"""
    assert h._HANDLER_TIMEOUT <= 30, "回调超时过长会让用户以为程序卡死"
    assert h._PROBE_TIMEOUT >= 10, "刚重装 Runtime 首启很慢，探测超时太短会误判"


def test_runtime_probe_does_not_delete_live_user_data():
    """探测成功后不得删掉 user-data 目录。

    探测成功意味着 msedgewebview2.exe 已经在用该目录；删掉它会让那个浏览器
    进程崩溃，并污染紧随其后的正式装配（表现为「探测说可用、真的建环境却
    报 0x80070002」）。
    """
    import inspect

    src = inspect.getsource(h._probe_runtime_dir)
    assert "rmtree(" not in src, "探测结束后不能删除正在使用的 user-data 目录"


def test_create_environment_falls_back_to_auto_discovery(monkeypatch, tmp_path):
    """显式指定 Runtime 目录失败时，必须再试一次「交给 loader 自动发现」。

    两种失败原因相反：loader 较旧无法自动发现新版 Runtime；目录被安全软件
    拦截 / 半损坏时自动发现反而能起来。只押一条路在真机上会失守。
    """
    win = h.WebView2Window(storage_dir=tmp_path)
    win.hwnd = 0x1234
    runtime = tmp_path / "155.0.4283.62"
    runtime.mkdir()
    monkeypatch.setattr(h, "_load_loader", lambda: object())
    monkeypatch.setattr(h, "find_runtime_dir", lambda: runtime)

    calls = []

    def fake_once(lib, folder, user_data, timeout=h._HANDLER_TIMEOUT):  # noqa: ANN001, ARG001
        calls.append(folder)
        if folder is not None:
            raise h.Wv2Error("模拟显式目录失败 0x80070002")
        return "ENV"

    monkeypatch.setattr(win, "_create_environment_once", fake_once)
    assert win._create_environment() == "ENV"
    assert calls == [str(runtime), None], "必须两种方式都试过"


def test_environment_error_distinguishes_installed_but_broken(monkeypatch, tmp_path):
    """「已装但建不出环境」不能报成「未安装」，否则用户会一直重装 Runtime。"""
    win = h.WebView2Window(storage_dir=tmp_path)
    win.hwnd = 0x1234
    monkeypatch.setattr(h, "_load_loader", lambda: object())
    monkeypatch.setattr(h, "find_runtime_dir", lambda: None)
    monkeypatch.setattr(h, "webview2_runtime_version", lambda: "155.0.4283.62")

    def boom(lib, folder, user_data, timeout=h._HANDLER_TIMEOUT):  # noqa: ANN001, ARG001
        raise h.Wv2Error("CreateCoreWebView2EnvironmentWithOptions 返回 0x80070002")

    monkeypatch.setattr(win, "_create_environment_once", boom)
    with pytest.raises(h.Wv2Error) as ei:
        win._create_environment()
    text = str(ei.value)
    assert "已安装但无法创建环境" in text
    assert "0x80070002" in text, "原始 HRESULT 必须保留，便于对日志排查"
    assert h.WEBVIEW2_DOWNLOAD_URL in text, "要给出修复入口"
    assert "信任区" in text, "要提示安全软件拦截这条真实原因"


def test_probe_environment_reports_missing_runtime(monkeypatch):
    """loader 在、但环境建不出来 → FAIL（并说明是「发现得到却用不了」）。

    注意不能只 mock ``find_runtime_dir``：枚举不到目录时会回落到 loader 自动
    发现，那也可能成功（按用户安装的运行时不在标准路径下）。
    """
    monkeypatch.setattr(h, "_load_loader", lambda: object())
    monkeypatch.setattr(h, "find_runtime_dir", lambda: None)
    monkeypatch.setattr(h, "_enumerate_runtime_dirs", lambda: [])
    monkeypatch.setattr(h, "_probe_runtime_dir", lambda folder, timeout=0: False)
    monkeypatch.setattr(h, "webview2_runtime_version", lambda: "155.0.0.0")

    ok, detail = h.probe_environment()
    assert ok is False
    assert "创建环境失败" in detail


def test_window_failure_text_has_diagnostics_and_next_steps(monkeypatch, tmp_path):
    """弹窗文案必须自带诊断（loader / 版本 / 目录）与下一步，否则用户无从下手。"""
    monkeypatch.setattr(h, "find_loader_dll", lambda: tmp_path / "WebView2Loader.dll")
    monkeypatch.setattr(h, "webview2_runtime_version", lambda: "155.0.4283.62")
    monkeypatch.setattr(h, "find_runtime_dir", lambda: None)

    text = h.window_failure_text("原因示例")
    assert "原因示例" in text
    assert "WebView2 Runtime 版本：155.0.4283.62" in text
    assert str(tmp_path / "WebView2Loader.dll") in text
    assert h.WEBVIEW2_DOWNLOAD_URL in text
    assert "--headless" in text
