"""Cookie 配置 API（/api/cookies）测试。

覆盖：状态查询、Netscape/JSON 两种导入、原子落盘与权限、关键 cookie 命中判定、
解析失败与超限的错误分支、删除，以及最容易出事的一条——**响应里绝不能出现 cookie 值**。
"""

import json
import os
import stat
import time

NETSCAPE = (
    "# Netscape HTTP Cookie File\n"
    "# https://curl.se/docs/http-cookies.html\n"
    "\n"
    ".xiaohongshu.com\tTRUE\t/\tFALSE\t0\tweb_session\tSECRET-SESSION-VALUE\n"
    "#HttpOnly_.xiaohongshu.com\tTRUE\t/\tFALSE\t0\ta1\tSECRET-A1-VALUE\n"
    ".xiaohongshu.com\tTRUE\t/\tFALSE\t4102444800\tgid\tSECRET-GID-VALUE\n"
)

JSON_EXPORT = json.dumps(
    [
        {
            "domain": ".xiaohongshu.com",
            "name": "web_session",
            "value": "JSON-SECRET-VALUE",
            "path": "/",
            "secure": True,
            "httpOnly": True,
            "expirationDate": 4102444800.123,
        },
        {
            "domain": ".xiaohongshu.com",
            "name": "a1",
            "value": "JSON-A1",
            "path": "/",
        },
    ]
)


async def _cookie_dir(app):
    from pathlib import Path

    return Path(app.state.components["settings"].cookie_dir)


async def test_get_lists_all_platforms_unconfigured(client):
    r = await client.get("/api/cookies")
    assert r.status_code == 200
    d = r.json()
    assert d["platforms"], "应返回平台列表"
    keys = [p["key"] for p in d["platforms"]]
    assert keys == ["douyin", "xhs", "bilibili", "youtube"]  # 与 COOKIE_FILES 同序
    xhs = next(p for p in d["platforms"] if p["key"] == "xhs")
    assert xhs["configured"] is False
    assert xhs["required"] is True
    assert xhs["label"] == "小红书"


async def test_put_netscape_saves_and_reports(client, app):
    r = await client.put("/api/cookies/xhs", json={"content": NETSCAPE})
    assert r.status_code == 200
    body = r.json()
    assert body["saved"] == "xhs"
    assert body["format"] == "netscape"
    assert body["count"] == 3

    st = body["status"]
    assert st["configured"] is True
    assert st["count"] == 3
    assert st["key_cookies_found"] == ["web_session", "a1"]
    assert st["key_cookies_missing"] == []
    assert st["domains"] == [".xiaohongshu.com"]
    assert st["session_cookies"] == 2  # 两条 expires=0 的会话 cookie
    assert st["expired"] is False  # 有 expires 的那条在 2100 年

    # 落点 = 抓取链路读的位置与文件名
    path = await _cookie_dir(app) / "xhs.txt"
    assert path.is_file()
    text = path.read_text(encoding="utf-8")
    assert text.startswith("# Netscape HTTP Cookie File")
    assert "#HttpOnly_.xiaohongshu.com" in text  # httpOnly 标记不能丢（丢了可能被判未登录）


async def test_saved_file_is_readable_by_fetcher(client, app):
    """保存后必须能被抓取链路直接读到（cookie_locator 与设置页共用同一目录）。"""
    from app.core.fetchers.ytdlp import cookie_args_for

    await client.put("/api/cookies/xhs", json={"content": NETSCAPE})
    cookie_dir = str(await _cookie_dir(app))
    args = cookie_args_for(cookie_dir, "xhs")
    assert args == [f"--cookies={cookie_dir}/xhs.txt"]


async def test_put_json_export_converted_to_netscape(client, app):
    r = await client.put("/api/cookies/xhs", json={"content": JSON_EXPORT})
    assert r.status_code == 200
    body = r.json()
    assert body["format"] == "json"
    assert body["count"] == 2

    text = (await _cookie_dir(app) / "xhs.txt").read_text(encoding="utf-8")
    # JSON 已转成 Netscape 文本，且 httpOnly 走了 #HttpOnly_ 前缀
    assert "JSON-SECRET-VALUE" in text
    assert "#HttpOnly_.xiaohongshu.com" in text
    assert "\t4102444800\t" in text  # expirationDate 浮点被取整落地


async def test_response_never_leaks_cookie_values(client):
    """接口只回元信息：任何响应体里都不允许出现 cookie 的 value。"""
    put = await client.put("/api/cookies/xhs", json={"content": NETSCAPE})
    assert "SECRET-SESSION-VALUE" not in put.text
    assert "SECRET-A1-VALUE" not in put.text
    got = await client.get("/api/cookies")
    assert "SECRET" not in got.text
    deleted = await client.delete("/api/cookies/xhs")
    assert "SECRET" not in deleted.text


async def test_file_permissions_are_owner_only(client, app):
    await client.put("/api/cookies/xhs", json={"content": NETSCAPE})
    path = await _cookie_dir(app) / "xhs.txt"
    mode = stat.S_IMODE(os.stat(path).st_mode)
    assert mode == 0o600, f"cookie 文件权限应为 600，实际 {oct(mode)}"


async def test_expired_cookie_is_flagged(client):
    expired = (
        "# Netscape HTTP Cookie File\n"
        ".xiaohongshu.com\tTRUE\t/\tFALSE\t1000000000\tweb_session\tOLD\n"
    )
    r = await client.put("/api/cookies/xhs", json={"content": expired})
    st = r.json()["status"]
    assert st["expired"] is True
    assert st["earliest_expiry"] == 1000000000
    assert st["earliest_expiry"] < time.time()


async def test_missing_key_cookie_is_reported(client):
    """只有无关 cookie 时，提示缺少登录凭证（而不是笼统说「已配置」）。"""
    partial = "# Netscape HTTP Cookie File\n.xiaohongshu.com\tTRUE\t/\tFALSE\t0\tfoo\tbar\n"
    r = await client.put("/api/cookies/xhs", json={"content": partial})
    st = r.json()["status"]
    assert st["configured"] is True
    assert st["key_cookies_found"] == []
    assert st["key_cookies_missing"] == ["web_session", "a1"]


async def test_delete_removes_file(client, app):
    await client.put("/api/cookies/xhs", json={"content": NETSCAPE})
    path = await _cookie_dir(app) / "xhs.txt"
    assert path.is_file()

    r = await client.delete("/api/cookies/xhs")
    assert r.status_code == 200
    assert r.json()["removed"] is True
    assert r.json()["status"]["configured"] is False
    assert not path.exists()

    # 幂等：再删一次不报错
    r2 = await client.delete("/api/cookies/xhs")
    assert r2.status_code == 200
    assert r2.json()["removed"] is False


async def test_platform_is_validated(client):
    """平台白名单：写进非法名字不能形成任意文件写入。"""
    r = await client.put("/api/cookies/../../etc/passwd", json={"content": NETSCAPE})
    assert r.status_code in (404, 405)
    r2 = await client.put("/api/cookies/foo", json={"content": NETSCAPE})
    assert r2.status_code == 404


async def test_unparsable_content_rejected(client):
    r = await client.put("/api/cookies/xhs", json={"content": "这不是 cookie 文件"})
    assert r.status_code == 422
    assert "Netscape" in r.json()["detail"]

    r2 = await client.put("/api/cookies/xhs", json={"content": "   "})
    assert r2.status_code == 422

    r3 = await client.put("/api/cookies/xhs", json={"content": "[{\"foo\": 1}]"})
    assert r3.status_code == 422


async def test_oversize_content_rejected(client):
    from app.api.cookies import MAX_COOKIE_BYTES

    big = NETSCAPE + "#" + "x" * MAX_COOKIE_BYTES
    r = await client.put("/api/cookies/xhs", json={"content": big})
    assert r.status_code == 413


# ============ 免扩展路径之一：Cookie 请求头 / F12 复制为 cURL ============

CURL_CMD = (
    "curl 'https://edith.xiaohongshu.com/api/sns/web/v1/feed' \\\n"
    "  -H 'accept: application/json' \\\n"
    "  -H 'cookie: a1=CURL-A1; web_session=CURL-SESSION; xsecappid=xhs-pc-web' \\\n"
    "  -H 'user-agent: Mozilla/5.0'"
)


async def test_put_cookie_header_synthesizes_platform_domain(client, app):
    """粘贴「Cookie: a=1; b=2」一行即可：自动补平台域，无需扩展。"""
    r = await client.put(
        "/api/cookies/xhs", json={"content": "Cookie: web_session=H-SESSION; a1=H-A1"}
    )
    assert r.status_code == 200
    body = r.json()
    assert body["format"] == "header"
    assert body["count"] == 2
    assert body["assumed_domain"] == ".xiaohongshu.com"
    assert body["status"]["key_cookies_found"] == ["web_session", "a1"]

    text = (await _cookie_dir(app) / "xhs.txt").read_text(encoding="utf-8")
    assert ".xiaohongshu.com\tTRUE\t/" in text
    assert "H-SESSION" in text


async def test_put_document_cookie_string_works(client):
    """document.cookie 那种没有前缀的 name=value; 串同样识别。"""
    r = await client.put("/api/cookies/xhs", json={"content": "web_session=DC; a1=DC2"})
    assert r.status_code == 200
    assert r.json()["format"] == "header"


async def test_put_curl_extracts_cookie_header(client, app):
    """F12「复制为 cURL」整条粘进来也能取到 Cookie 头（跳过其它 -H）。"""
    r = await client.put("/api/cookies/xhs", json={"content": CURL_CMD})
    assert r.status_code == 200
    body = r.json()
    assert body["format"] == "curl"
    assert body["count"] == 3
    text = (await _cookie_dir(app) / "xhs.txt").read_text(encoding="utf-8")
    assert "CURL-SESSION" in text
    assert "xsecappid" in text


async def test_secure_prefixed_cookie_marked_secure(client, app):
    """__Secure- / __Host- 前缀按规范置 secure=TRUE，否则部分实现会丢弃。"""
    r = await client.put(
        "/api/cookies/youtube", json={"content": "__Secure-1PSID=S; SID=plain"}
    )
    assert r.status_code == 200
    text = (await _cookie_dir(app) / "youtube.txt").read_text(encoding="utf-8")
    secure_line = next(ln for ln in text.splitlines() if "__Secure-1PSID" in ln)
    plain_line = next(ln for ln in text.splitlines() if "\tSID\t" in ln)
    # Netscape 字段：domain / flag / path / secure / expires / name / value
    assert secure_line.split("\t")[3] == "TRUE"
    assert plain_line.split("\t")[3] == "FALSE"


async def test_curl_without_cookie_header_rejected(client):
    """-b cookies.txt 这类没有内联 cookie 的命令要给出可读错误，而不是静默空文件。"""
    r = await client.put(
        "/api/cookies/xhs", json={"content": "curl 'https://x.com' -b cookies.txt"}
    )
    assert r.status_code == 422
    assert "Cookie" in r.json()["detail"]


# ============ 免扩展路径之二：从服务器本机浏览器读 ============


class _FakeBrowserCookie:
    def __init__(self, domain, name, value, secure=False, expires=0, http_only=False, path="/"):
        self.domain, self.name, self.value = domain, name, value
        self.path, self.secure, self.expires = path, secure, expires
        self._rest = {"HttpOnly": True} if http_only else {}


async def test_browsers_list_reports_availability(client):
    r = await client.get("/api/cookies/browsers")
    assert r.status_code == 200
    browsers = r.json()["browsers"]
    assert [b["key"] for b in browsers][:4] == ["chrome", "chromium", "edge", "firefox"]
    assert all(isinstance(b["available"], bool) for b in browsers)


async def test_from_browser_keeps_only_platform_domain(client, app, monkeypatch):
    """从浏览器整库导入时，只落该平台的 cookie（不把别人站点的登录态写进项目）。"""
    jar = [
        _FakeBrowserCookie(".xiaohongshu.com", "web_session", "XHS-VALUE", http_only=True),
        _FakeBrowserCookie(".xiaohongshu.com", "a1", "XHS-A1"),
        _FakeBrowserCookie(".douyin.com", "sessionid", "DOUYIN-VALUE"),
        _FakeBrowserCookie("www.google.com", "NID", "GOOGLE-VALUE"),
    ]
    monkeypatch.setattr("yt_dlp.cookies.extract_cookies_from_browser", lambda browser: jar)

    r = await client.post("/api/cookies/xhs/from-browser", json={"browser": "chromium"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["count"] == 2
    assert body["source"] == "browser:chromium"
    assert body["status"]["key_cookies_found"] == ["web_session", "a1"]
    assert "XHS-VALUE" not in r.text  # 依旧不回传 value

    text = (await _cookie_dir(app) / "xhs.txt").read_text(encoding="utf-8")
    assert "XHS-VALUE" in text
    assert "#HttpOnly_.xiaohongshu.com" in text  # httpOnly 标记保留
    assert "DOUYIN-VALUE" not in text and "GOOGLE-VALUE" not in text


async def test_from_browser_no_matching_cookie_is_actionable(client, monkeypatch):
    monkeypatch.setattr(
        "yt_dlp.cookies.extract_cookies_from_browser",
        lambda browser: [_FakeBrowserCookie(".example.com", "x", "y")],
    )
    r = await client.post("/api/cookies/xhs/from-browser", json={"browser": "firefox"})
    assert r.status_code == 422
    assert "登录" in r.json()["detail"]


async def test_from_browser_failure_surfaces_ytdlp_message(client, monkeypatch):
    """没装浏览器 / 解密失败时，把 yt-dlp 的原始报错透传给用户。"""

    def boom(browser):
        raise RuntimeError("could not find firefox cookies database in /home/app/.mozilla")

    monkeypatch.setattr("yt_dlp.cookies.extract_cookies_from_browser", boom)
    r = await client.post("/api/cookies/xhs/from-browser", json={"browser": "firefox"})
    assert r.status_code == 422
    detail = r.json()["detail"]
    assert "could not find firefox cookies database" in detail
    assert "复制为 cURL" in detail  # 附带可执行的替代方案


async def test_from_browser_rejects_unknown_browser(client):
    r = await client.post("/api/cookies/xhs/from-browser", json={"browser": "netscape"})
    assert r.status_code == 404
