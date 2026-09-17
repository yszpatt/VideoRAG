"""品牌标识资源（logo / favicon）测试。

这个文件的存在是有原因的：logo 曾**两次**因为 SVG 自身的问题在界面上静默消失——
浏览器不给任何报错、控制台也干净，只是什么都不显示，排查成本极高。两次的原因分别是：

1. 注释里写了中文 → 前端打包器把小 SVG 内联成 ``data:`` URI，而非 ASCII 字符在该
   形态下需要百分号编码，工具链未必会做，浏览器可能直接拒绝渲染；
2. 注释里写了 CSS 变量名（以两个连字符开头）→ **XML 注释不允许出现连续两个连字符**，
   整个文档因此不是合法 XML，浏览器拒绝渲染整个 SVG。

两条都在这里钉死，避免第三次。生成侧另有 ``packaging/make_logo.py`` 的自校验。
"""
from __future__ import annotations

import pathlib
import xml.etree.ElementTree as ET

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent
SVG_NS = "{http://www.w3.org/2000/svg}"

LOGOS = (
    ROOT / "web" / "src" / "assets" / "logo.svg",
    ROOT / "web" / "public" / "favicon.svg",
)


@pytest.mark.parametrize("path", LOGOS, ids=lambda p: p.name)
def test_logo_svg_is_valid_xml(path):
    """必须是合法 XML——注释里的连续连字符会让浏览器拒绝渲染整个 SVG。"""
    assert path.is_file(), f"缺少 {path}"
    ET.fromstring(path.read_text(encoding="utf-8"))


@pytest.mark.parametrize("path", LOGOS, ids=lambda p: p.name)
def test_logo_svg_is_pure_ascii(path):
    """必须纯 ASCII——会被内联成 data URI，非 ASCII 在该形态下不可靠。"""
    text = path.read_text(encoding="utf-8")
    bad = sorted({c for c in text if ord(c) > 127})
    assert not bad, f"{path.name} 含非 ASCII 字符 {bad}"


@pytest.mark.parametrize("path", LOGOS, ids=lambda p: p.name)
def test_logo_svg_has_no_double_hyphen_in_comments(path):
    """直接把规则写成断言，让失败信息点明原因（比 XML 解析报错更易读）。"""
    import re

    text = path.read_text(encoding="utf-8")
    comments = re.findall(r"<!--(.*?)-->", text, re.S)
    offenders = [c.strip()[:60] for c in comments if "--" in c]
    assert not offenders, f"{path.name} 的注释含连续连字符：{offenders}"


def test_logo_svg_keeps_expected_shapes():
    """重生成时若丢元素，这里会立刻发现。"""
    root = ET.fromstring((ROOT / "web" / "src" / "assets" / "logo.svg").read_text(encoding="utf-8"))
    for tag in ("polygon", "rect", "linearGradient"):
        assert root.find(f".//{SVG_NS}{tag}") is not None, f"logo 缺少 {tag}"


def test_icon_and_display_assets_exist():
    """exe 图标与展示图必须存在：图标缺了窗口会顶着解释器默认图标。"""
    logo_dir = ROOT / "packaging" / "logo"
    for name in ("videorag.ico", "videorag-256.png", "videorag-1024.png"):
        assert (logo_dir / name).is_file(), f"缺少 packaging/logo/{name}"


def test_favicon_links_declared_in_index_html():
    """index.html 要真的引用 favicon，否则浏览器标签页还是白板图标。"""
    html = (ROOT / "web" / "index.html").read_text(encoding="utf-8")
    assert 'rel="icon"' in html and "/favicon.svg" in html
