"""Cookie 配置 API（设置页「Cookie / 登录」页签）。

- GET    /api/cookies                   各平台状态（条数/域/关键 cookie/过期；**不回传内容**）
- PUT    /api/cookies/{platform}        {content} 保存
- DELETE /api/cookies/{platform}        清除该平台 cookie 文件
- GET    /api/cookies/browsers          本机检测到的浏览器（「从浏览器导入」用）
- POST   /api/cookies/{platform}/from-browser  {browser} 直接读本机浏览器 cookie 库

**免扩展**的获取方式（都不需要装浏览器插件）：

1. F12 → 网络 → 任一请求 → 「复制为 cURL」→ 粘贴（自动取 Cookie 头）；
2. F12 → 网络 → 请求头 → 复制 ``Cookie:`` 那一行 → 粘贴；
3. 「从浏览器导入」：服务器本机有浏览器 profile 时直接读（桌面 / 裸机形态；
   容器里读不到宿主浏览器）。

设计要点：

- **平台白名单**复用 ``app.core.fetchers.ytdlp.COOKIE_FILES``，与抓取链路同一份口径，
  避免「设置页能配、抓取不认」这类漂移；
- **落点**是 ``<COOKIE_DIR>/<platform>.txt``，正是 ``YtdlpFetcher`` / ``cookie_args_for``
  读取的位置；抓取时现读文件，因此保存后**无需重启**，下一次提交任务即生效；
- **只回传元信息**（条数、域、关键 cookie 是否命中、最早过期时间），**绝不回传 value**：
  cookie 等同登录凭据，回传会进浏览器缓存 / 日志 / 截图；
- 写入走临时文件 + ``os.replace`` 原子替换，权限 0600（cookie 是账号级别秘密）；
- 从浏览器导入时**只落该平台的 cookie**，不把整库（含其它站点登录态）写进项目。
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import time
from dataclasses import dataclass
from pathlib import Path

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from app.core.fetchers.ytdlp import COOKIE_FILES

router = APIRouter(prefix="/api/cookies")

# 单次提交上限：正常 cookie 导出在几 KB~几十 KB，512KB 足够且能挡住误传大文件
MAX_COOKIE_BYTES = 512 * 1024

# 平台元信息：key 必须与 COOKIE_FILES 一致（下方导入时校验一次）
# - default_domain：从「Cookie 请求头 / cURL」这类没有域信息的输入合成 cookie 时用的域
# - domains：从浏览器整库导入时，用它挑出该平台的 cookie（域名后缀匹配）
PLATFORMS: dict[str, dict] = {
    "xhs": {
        "label": "小红书",
        "required": True,
        "key_cookies": ["web_session", "a1"],
        "note": "必需：未配置时提交小红书链接会被直接拒绝（未登录拿不到视频流）",
        "default_domain": ".xiaohongshu.com",
        "domains": ["xiaohongshu.com"],
    },
    "douyin": {
        "label": "抖音",
        "required": True,
        "key_cookies": ["sessionid", "sessionid_ss"],
        "note": "必需：未配置时提交抖音链接会被直接拒绝",
        "default_domain": ".douyin.com",
        "domains": ["douyin.com", "iesdouyin.com"],
    },
    "bilibili": {
        "label": "B 站",
        "required": False,
        "key_cookies": ["SESSDATA", "bili_jct"],
        "note": "可选：登录后才有 1080P+ 画质、CC 字幕与会员 / 私有内容（SESSDATA 是登录凭证）",
        "default_domain": ".bilibili.com",
        "domains": ["bilibili.com"],
    },
    "youtube": {
        "label": "YouTube",
        "required": False,
        "key_cookies": ["SID", "SAPISID", "__Secure-1PSID"],
        "note": "可选：年龄限制 / 会员 / 私有内容必需；导出方式较讲究（见文档，需避免 cookie 轮换）",
        "default_domain": ".youtube.com",
        "domains": ["youtube.com", "google.com", "googlevideo.com"],
    },
}

# 支持的浏览器（取值与 yt-dlp --cookies-from-browser 一致）
BROWSERS: list[dict] = [
    {"key": "chrome", "label": "Chrome"},
    {"key": "chromium", "label": "Chromium"},
    {"key": "edge", "label": "Edge"},
    {"key": "firefox", "label": "Firefox"},
    {"key": "brave", "label": "Brave"},
    {"key": "vivaldi", "label": "Vivaldi"},
    {"key": "opera", "label": "Opera"},
    {"key": "safari", "label": "Safari"},
]

# 导入期一致性校验：白名单与抓取链路口径必须完全一致
assert set(PLATFORMS) == set(COOKIE_FILES), "PLATFORMS 与 COOKIE_FILES 不一致"


@dataclass
class _Cookie:
    domain: str
    name: str
    value: str
    path: str = "/"
    secure: bool = False
    expires: int = 0
    http_only: bool = False


def _cookie_dir(request: Request) -> Path:
    return Path(request.app.state.components["settings"].cookie_dir)


def _parse_netscape(text: str) -> list[_Cookie]:
    """解析 Netscape cookie 文本；``#HttpOnly_`` 前缀行是 cookie 而非注释。"""
    out: list[_Cookie] = []
    for raw in text.splitlines():
        line = raw.rstrip("\r")
        if not line.strip():
            continue
        http_only = False
        if line.startswith("#HttpOnly_"):
            http_only = True
            line = line[len("#HttpOnly_") :]
        elif line.lstrip().startswith("#"):
            continue
        parts = line.split("\t")
        if len(parts) < 7:
            continue  # 非 cookie 行（如插件附带的说明文本）
        domain, _flag, path, secure, expires, name = parts[:6]
        value = "\t".join(parts[6:])  # value 里理论上不含 tab，兜底拼接保完整
        try:
            exp = int(float(expires))
        except (TypeError, ValueError):
            exp = 0
        out.append(
            _Cookie(
                domain=domain.strip(),
                name=name.strip(),
                value=value,
                path=path.strip() or "/",
                secure=secure.strip().upper() == "TRUE",
                expires=exp,
                http_only=http_only,
            )
        )
    return out


def _parse_json_export(text: str) -> list[_Cookie]:
    """解析扩展导出的 JSON（Cookie-Editor / EditThisCookie / DevTools 风格）。"""
    data = json.loads(text)
    if isinstance(data, dict):
        data = data.get("cookies") or []
    if not isinstance(data, list):
        raise ValueError("JSON 根节点应为 cookie 数组（或含 cookies 数组的对象）")
    out: list[_Cookie] = []
    for item in data:
        if not isinstance(item, dict):
            continue
        name = item.get("name")
        value = item.get("value")
        if not name or value is None:
            continue
        expires = item.get("expirationDate", item.get("expires", 0))
        try:
            exp = int(float(expires)) if expires else 0
        except (TypeError, ValueError):
            exp = 0
        out.append(
            _Cookie(
                domain=str(item.get("domain") or "").strip(),
                name=str(name).strip(),
                value=str(value),
                path=str(item.get("path") or "/"),
                secure=bool(item.get("secure")),
                expires=exp,
                http_only=bool(item.get("httpOnly")),
            )
        )
    return out


def _split_cookie_header(header: str) -> list[tuple[str, str]]:
    """拆 ``a=1; b=2`` 形式的 Cookie 请求头 / ``document.cookie`` 字符串。"""
    body = header.strip()
    if body[:7].lower() == "cookie:":
        body = body[7:]
    out: list[tuple[str, str]] = []
    for chunk in body.split(";"):
        chunk = chunk.strip()
        if not chunk or "=" not in chunk:
            continue
        name, _, value = chunk.partition("=")
        name, value = name.strip(), value.strip()
        if name:
            out.append((name, value))
    return out


def _synth_cookies(pairs: list[tuple[str, str]], domain: str) -> list[_Cookie]:
    """把 ``name=value`` 对合成为该平台的 cookie（用平台默认域、会话 Cookie）。

    ``__Secure-`` / ``__Host-`` 前缀按规范要求 secure=True，否则部分实现会丢弃。
    """
    return [
        _Cookie(
            domain=domain,
            name=name,
            value=value,
            secure=name.startswith("__Secure-") or name.startswith("__Host-"),
        )
        for name, value in pairs
    ]


# F12「复制为 cURL」里的 cookie：-H 'cookie: …' / --header 'Cookie: …' / -b '…'
_CURL_COOKIE_RE = re.compile(
    r"""(?:-H|--header)\s+['"](?:cookie)\s*:\s*(?P<header>[^'"]*)['"]"""
    r"""|(?:-b|--cookie)\s+['"](?P<body>[^'"]*)['"]""",
    re.IGNORECASE,
)


def _parse_cookie_header(text: str, domain: str) -> list[_Cookie]:
    """「Cookie: a=1; b=2」请求头（或 ``document.cookie`` 的字符串）。

    这是**不需要任何浏览器扩展**的手工路径：F12 → 网络 → 任一请求 → 请求头里的
    ``Cookie:`` 一行（含 HttpOnly，比 ``document.cookie`` 更全）复制过来即可。
    """
    return _synth_cookies(_split_cookie_header(text), domain)


def _parse_curl(text: str, domain: str) -> list[_Cookie]:
    """解析 F12「复制为 cURL」命令里的 Cookie 头。"""
    m = _CURL_COOKIE_RE.search(text)
    if not m:
        return []
    raw = m.group("header")
    if raw is None:
        raw = m.group("body") or ""
    return _synth_cookies(_split_cookie_header(raw), domain)


def parse_cookie_text(text: str, platform: str) -> tuple[list[_Cookie], str, str]:
    """解析任意受支持的输入，返回 ``(cookies, 格式名, 使用的域)``。

    按顺序尝试（每一步都要求此前缀特征，避免误判）：

    1. **JSON**：扩展导出（Cookie-Editor / EditThisCookie）；
    2. **cURL**：F12「复制为 cURL」，从中取 ``Cookie:`` 头；
    3. **Cookie 请求头 / document.cookie**：``a=1; b=2`` 形式；
    4. **Netscape 文本**：``# Netscape HTTP Cookie File``。

    2/3 两种输入没有域信息，按平台默认域合成（如 ``.xiaohongshu.com``），
    并把该域作为第三项返回，便于调用方在响应里说明「假定域」。
    """
    meta = PLATFORMS[platform]
    domain = meta["default_domain"]
    stripped = text.strip()

    if stripped.startswith("[") or stripped.startswith("{"):
        try:
            cookies = _parse_json_export(text)
        except (ValueError, json.JSONDecodeError) as e:
            raise ValueError(f"JSON 解析失败：{e}") from e
        if not cookies:
            raise ValueError("JSON 里没有可用的 cookie 条目")
        return cookies, "json", ""

    # Netscape 是 tab 分隔的；没有 tab 才可能是「头 / cURL」这类单行输入
    if "\t" not in text:
        head = stripped[:400].lower()
        if "curl " in head or "-h " in head or "-b " in head or "--header" in head:
            cookies = _parse_curl(text, domain)
            if not cookies:
                raise ValueError("这条 cURL 里没找到 Cookie 头（复制时请选「复制为 cURL (bash)」）")
            return cookies, "curl", domain
        if "=" in stripped:
            cookies = _parse_cookie_header(text, domain)
            if cookies:
                return cookies, "header", domain

    cookies = _parse_netscape(text)
    if not cookies:
        raise ValueError(
            "没解析出任何 cookie。支持：扩展导出的 Netscape 文本 / JSON、"
            "F12 的「复制为 cURL」、请求头里的 Cookie: 一行。"
        )
    return cookies, "netscape", ""


def _to_netscape(cookies: list[_Cookie]) -> str:
    lines = [
        "# Netscape HTTP Cookie File",
        "# 由 videoRAG 设置页写入（Cookie / 登录）",
        "",
    ]
    for c in cookies:
        prefix = "#HttpOnly_" if c.http_only else ""
        domain_flag = "TRUE" if c.domain.startswith(".") else "FALSE"
        lines.append(
            "\t".join(
                [
                    f"{prefix}{c.domain}",
                    domain_flag,
                    c.path or "/",
                    "TRUE" if c.secure else "FALSE",
                    str(int(c.expires or 0)),
                    c.name,
                    c.value,
                ]
            )
        )
    return "\n".join(lines) + "\n"


def _status(platform: str, path: Path) -> dict:
    """某平台 cookie 的状态：只暴露元信息，绝不包含 value。"""
    meta = PLATFORMS[platform]
    item: dict = {
        "key": platform,
        "label": meta["label"],
        "required": meta["required"],
        "note": meta["note"],
        "key_cookies": meta["key_cookies"],
        "path": str(path),
        "configured": False,
    }
    if not path.is_file():
        return item

    stat = path.stat()
    item.update(
        configured=True,
        size=stat.st_size,
        saved_at=time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(stat.st_mtime)),
    )
    try:
        cookies, fmt, _domain = parse_cookie_text(
            path.read_text(encoding="utf-8", errors="replace"), platform
        )
    except (OSError, ValueError) as e:
        # 文件在，但内容不可解析（手工编辑坏了）——如实报告，别假装已配置可用
        item.update(parse_error=str(e))
        return item

    names = {c.name for c in cookies}
    found = [n for n in meta["key_cookies"] if n in names]
    expiries = [c.expires for c in cookies if c.expires and c.expires > 0]
    now = int(time.time())
    item.update(
        format=fmt,
        count=len(cookies),
        domains=sorted({c.domain for c in cookies if c.domain}),
        key_cookies_found=found,
        key_cookies_missing=[n for n in meta["key_cookies"] if n not in names],
        earliest_expiry=min(expiries) if expiries else 0,
        expired=bool(expiries) and min(expiries) <= now,
        session_cookies=sum(1 for c in cookies if not c.expires),
    )
    return item


def _validate_platform(platform: str) -> str:
    key = (platform or "").strip().lower()
    if key not in PLATFORMS:
        raise HTTPException(404, f"未知平台：{platform}（可选：{', '.join(PLATFORMS)}）")
    return key


def _write_cookies(path: Path, cookies: list[_Cookie]) -> None:
    """原子写入 cookie 文件（临时文件 + os.replace，权限 0600）。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    # 先按 0600 建文件再写，避免「先 0644 后 chmod」的窗口期把凭据暴露出去
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(_to_netscape(cookies))
        os.replace(tmp, path)
    except OSError as e:  # 磁盘满 / 权限不足：清掉临时文件再报错
        tmp.unlink(missing_ok=True)
        raise HTTPException(500, f"写入 cookie 文件失败：{e}") from e


@router.get("")
async def list_cookies(request: Request):
    """各平台 cookie 状态（不回传内容），供设置页展示。"""
    d = _cookie_dir(request)
    return {
        "cookie_dir": str(d),
        "platforms": [_status(k, d / f"{k}.txt") for k in COOKIE_FILES],
    }


class SaveCookieRequest(BaseModel):
    content: str


@router.put("/{platform}")
async def save_cookie(platform: str, req: SaveCookieRequest, request: Request):
    """保存某平台 cookie。

    接受四种输入：扩展导出的 Netscape 文本 / JSON、F12 的「复制为 cURL」、
    请求头里的 ``Cookie:`` 一行。原子写入并返回解析后的状态。
    """
    key = _validate_platform(platform)
    raw = req.content or ""
    if not raw.strip():
        raise HTTPException(422, "内容为空")
    if len(raw.encode("utf-8")) > MAX_COOKIE_BYTES:
        raise HTTPException(413, f"内容超过 {MAX_COOKIE_BYTES // 1024}KB 上限")

    try:
        cookies, fmt, assumed_domain = parse_cookie_text(raw, key)
    except ValueError as e:
        raise HTTPException(422, str(e)) from e

    path = _cookie_dir(request) / f"{key}.txt"
    _write_cookies(path, cookies)

    result = {
        "saved": key,
        "format": fmt,
        "count": len(cookies),
        "status": _status(key, path),
    }
    if assumed_domain:  # 头 / cURL 输入没有域信息，如实告知假定了什么
        result["assumed_domain"] = assumed_domain
    return result


@router.delete("/{platform}")
async def delete_cookie(platform: str, request: Request):
    """清除某平台 cookie 文件。"""
    key = _validate_platform(platform)
    path = _cookie_dir(request) / f"{key}.txt"
    removed = path.is_file()
    path.unlink(missing_ok=True)
    return {"removed": removed, "status": _status(key, path)}


# ============ 免扩展路径之二：直接从服务器本机浏览器读 ============

# 各浏览器的常见 profile 目录（只用于「检测到没有」的提示，不参与读取实现）
_BROWSER_DIRS: dict[str, list[str]] = {
    "firefox": [
        "~/.mozilla/firefox",
        "~/Library/Application Support/Firefox",
        "%APPDATA%/Mozilla/Firefox",
    ],
    "chrome": [
        "~/.config/google-chrome",
        "~/Library/Application Support/Google/Chrome",
        "%LOCALAPPDATA%/Google/Chrome/User Data",
    ],
    "chromium": [
        "~/.config/chromium",
        "~/Library/Application Support/Chromium",
        "%LOCALAPPDATA%/Chromium/User Data",
    ],
    "edge": [
        "~/.config/microsoft-edge",
        "~/Library/Application Support/Microsoft Edge",
        "%LOCALAPPDATA%/Microsoft/Edge/User Data",
    ],
    "brave": [
        "~/.config/BraveSoftware/Brave-Browser",
        "~/Library/Application Support/BraveSoftware/Brave-Browser",
        "%LOCALAPPDATA%/BraveSoftware/Brave-Browser/User Data",
    ],
    "vivaldi": [
        "~/.config/vivaldi",
        "~/Library/Application Support/Vivaldi",
        "%LOCALAPPDATA%/Vivaldi/User Data",
    ],
    "opera": [
        "~/.config/opera",
        "~/Library/Application Support/com.operasoftware.Opera",
        "%APPDATA%/Opera Software/Opera Stable",
    ],
    "safari": ["~/Library/Cookies", "~/Library/Safari"],
}


def _expand_profile_path(pattern: str) -> Path:
    for var in ("LOCALAPPDATA", "APPDATA"):
        pattern = pattern.replace(f"%{var}%", os.environ.get(var, ""))
    return Path(pattern).expanduser()


def browser_available(key: str) -> bool:
    return any(_expand_profile_path(p).is_dir() for p in _BROWSER_DIRS.get(key, []))


def extract_browser_cookies(browser: str, platform: str) -> list[_Cookie]:
    """用 yt-dlp 的实现读**本机浏览器** cookie 库（含解密），只保留该平台的域。

    只取该平台的 cookie 落盘：既不把整库（几百上千条、含其它站点登录态）
    写进项目，也避免用户以为「导入的只有这一个平台」。

    失败（没装浏览器 / profile 不在 / Chrome 新版 App-Bound 加密解不开）
    由调用方把 yt-dlp 的原始报错透传给用户——它通常已经指明缺什么。
    """
    from yt_dlp.cookies import extract_cookies_from_browser

    jar = extract_cookies_from_browser(browser)
    domains = PLATFORMS[platform]["domains"]
    out: list[_Cookie] = []
    for c in jar:
        dom = (c.domain or "").lstrip(".")
        if not any(dom == d or dom.endswith("." + d) for d in domains):
            continue
        rest = getattr(c, "_rest", {}) or {}
        out.append(
            _Cookie(
                domain=c.domain or "",
                name=c.name,
                value=c.value or "",
                path=c.path or "/",
                secure=bool(c.secure),
                expires=int(c.expires or 0),
                http_only=bool(rest.get("HttpOnly") or rest.get("HTTPOnly")),
            )
        )
    return out


@router.get("/browsers")
async def list_browsers():
    """本机检测到的浏览器（供「从浏览器导入」选择）。

    容器形态下通常全是 false：容器里没有宿主浏览器 profile，这是预期行为，
    前端据此提示改用粘贴 / cURL 方式。
    """
    return {
        "browsers": [
            {**b, "available": browser_available(b["key"])} for b in BROWSERS
        ]
    }


class FromBrowserRequest(BaseModel):
    browser: str


@router.post("/{platform}/from-browser")
async def import_from_browser(platform: str, req: FromBrowserRequest, request: Request):
    """从服务器本机浏览器直接读取该平台 cookie（免扩展、免复制粘贴）。"""
    key = _validate_platform(platform)
    browser = (req.browser or "").strip().lower()
    if browser not in {b["key"] for b in BROWSERS}:
        raise HTTPException(404, f"不支持的浏览器：{req.browser}")

    try:
        cookies = await asyncio.to_thread(extract_browser_cookies, browser, key)
    except Exception as e:  # noqa: BLE001  读库/解密失败的报错对用户最有价值，原样透传
        detail = str(e).splitlines()[0] if str(e) else e.__class__.__name__
        raise HTTPException(
            422,
            f"从 {browser} 读取 cookie 失败：{detail}。"
            f"提示：容器形态读不到宿主浏览器，请改用粘贴 / 复制为 cURL；"
            f"Chrome / Edge 新版本的 App-Bound 加密也可能导致解密失败（Firefox 最稳）。",
        ) from e

    if not cookies:
        raise HTTPException(
            422,
            f"{browser} 里没有 {PLATFORMS[key]['label']} 的 cookie——"
            f"请先在该浏览器登录，或确认登录的是同一份 profile。",
        )

    path = _cookie_dir(request) / f"{key}.txt"
    _write_cookies(path, cookies)
    return {
        "saved": key,
        "source": f"browser:{browser}",
        "count": len(cookies),
        "status": _status(key, path),
    }
