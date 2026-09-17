"""生成 videoRAG 品牌标识与各平台图标。

**为什么用脚本生成而不是直接放图片**：同一个标识要出现在 4 个地方，尺寸与格式各不相同
（Web 内联 SVG、浏览器 favicon、exe 图标、README 展示图）。手工维护多份必然漂移，
所以几何参数只定义一次，同时产出矢量与多尺寸位图。

设计说明
--------
- **形制**：圆角方形，圆角比例 31%（与 UI 里 `.brand-mark` 的 `32px / radius 10px` 一致）
- **配色**：直接复用 UI 主题的 `--grad`（135° 紫 `#7c3aed` → 粉 `#ec4899` → 橙 `#f97316`），
  使标识与界面同源，不必两处维护色值
- **图形**：白色播放三角（视频）+ 右侧两条递减竖条（音频波形 / 被切出的文字片段）。
  只用两个元素是刻意的——侧边栏里它只有 32px，元素一多就会糊成一团

产物
----
    web/src/assets/logo.svg        前端引用（vite 作 asset 处理）
    web/public/favicon.svg         浏览器标签页（矢量，优先）
    web/public/favicon.ico         浏览器标签页（旧浏览器回退）
    packaging/logo/videorag.ico    exe 图标（多尺寸打包，Windows 会按 DPI 选）
    packaging/logo/videorag-1024.png / -256.png   README 与商店展示

用法::

    python packaging/make_logo.py
"""
from __future__ import annotations

import pathlib
import xml.etree.ElementTree as ElementTree

import numpy as np
from PIL import Image, ImageChops, ImageDraw

# ---------------------------------------------------------------- 设计参数
# 统一在 512 坐标系里定义，位图按 size/512 缩放；改这里等于改所有产物
CANVAS = 512
PAD = 20          # 圆角方块的外边距（20/512 ≈ 4%，与 UI 中留白比例一致）
RADIUS = 160      # 圆角半径（160/512 = 31%，对应 32px 时的 10px）

# 与 web/src/styles.css 的 --grad 保持一致
GRAD_STOPS: list[tuple[float, tuple[int, int, int]]] = [
    (0.0, (124, 58, 237)),    # #7c3aed 紫
    (0.5, (236, 72, 153)),    # #ec4899 粉
    (1.0, (249, 115, 22)),    # #f97316 橙
]

# 播放三角（左顶点起，水平指向右）
# 图形整体占方块内宽约 63%、水平垂直都居中——太小显得空，太大在 32px 下会顶到圆角
TRIANGLE = [(106, 140), (106, 372), (282, 256)]
# 右侧波形条：(x, y, w, h)；高度递减，形成"声音衰减 / 片段缩短"的意象
BARS = [(320, 168, 32, 176), (372, 200, 32, 112)]

FG = "#ffffff"
FG_RGBA = (255, 255, 255, 255)

# 超采样倍率：先在 4x 画布上绘制再降采样，得到干净的抗锯齿边缘
SUPERSAMPLE = 4

ICO_SIZES = (16, 24, 32, 48, 64, 128, 256)
FAVICON_SIZES = (16, 32, 48)


# ---------------------------------------------------------------- 矢量版
def build_svg() -> str:
    """手写 SVG（与位图共用上面的参数，因此不会漂移）。"""
    side = CANVAS - 2 * PAD
    tri = " ".join(f"{x},{y}" for x, y in TRIANGLE)
    bars = "\n".join(
        f'  <rect x="{x}" y="{y}" width="{w}" height="{h}" rx="{w / 2}" '
        f'ry="{w / 2}" fill="{FG}"/>'
        for x, y, w, h in BARS
    )
    stops = "\n".join(
        f'    <stop offset="{round(offset * 100, 1)}%" '
        f'stop-color="#{r:02x}{g:02x}{b:02x}"/>'
        for offset, (r, g, b) in GRAD_STOPS
    )
    return f"""<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {CANVAS} {CANVAS}"
     role="img" aria-label="videoRAG">
  <title>videoRAG</title>
  <!--
    Maintainer notes. Each rule below has been violated once already, and every time
    the logo silently stopped rendering in the browser (no error, just nothing):

    1. Keep this file pure ASCII. The bundler inlines it as a data URI, and such URIs
       must be ASCII; otherwise they need percent-encoding that toolchains may skip.
    2. An XML comment must NOT contain two consecutive hyphens. Do not write CSS custom
       property names (they begin with two hyphens) inside a comment: that makes the
       document invalid XML and browsers refuse to render the whole SVG.
  -->
  <defs>
    <!-- Same colors as the UI theme gradient: 135deg violet, pink, orange -->
    <linearGradient id="vrag-grad" x1="0%" y1="0%" x2="100%" y2="100%">
{stops}
    </linearGradient>
  </defs>
  <rect x="{PAD}" y="{PAD}" width="{side}" height="{side}"
        rx="{RADIUS}" ry="{RADIUS}" fill="url(#vrag-grad)"/>
  <polygon points="{tri}" fill="{FG}"/>
{bars}
</svg>
"""


# ---------------------------------------------------------------- 位图版
def build_rgba(size: int) -> Image.Image:
    """渲染指定边长的 RGBA 图标。"""
    w = size * SUPERSAMPLE
    k = w / CANVAS

    # 渐变底：135° 方向用 (x+y)/(2*(w-1)) 投影，与 CSS linear-gradient(135deg) 同向
    yy, xx = np.mgrid[0:w, 0:w].astype(np.float32)
    t = (xx + yy) / (2 * (w - 1))
    offsets = [o for o, _ in GRAD_STOPS]
    channels = [
        np.interp(t, offsets, [c[i] for _, c in GRAD_STOPS]) for i in range(3)
    ]
    gradient = np.stack(channels, axis=-1).round().astype(np.uint8)
    base = Image.fromarray(gradient, "RGB").convert("RGBA")

    # 圆角方形的遮罩
    mask = Image.new("L", (w, w), 0)
    ImageDraw.Draw(mask).rounded_rectangle(
        [PAD * k, PAD * k, (CANVAS - PAD) * k, (CANVAS - PAD) * k],
        radius=RADIUS * k,
        fill=255,
    )

    # 前景图形，并裁进圆角方形内（防止任何越界）
    shape = Image.new("L", (w, w), 0)
    sd = ImageDraw.Draw(shape)
    sd.polygon([(x * k, y * k) for x, y in TRIANGLE], fill=255)
    for x, y, bw, bh in BARS:
        sd.rounded_rectangle(
            [x * k, y * k, (x + bw) * k, (y + bh) * k], radius=(bw / 2) * k, fill=255
        )
    shape = ImageChops.multiply(shape, mask)

    out = Image.new("RGBA", (w, w), (0, 0, 0, 0))
    out.paste(base, (0, 0), mask)
    out.paste(Image.new("RGBA", (w, w), FG_RGBA), (0, 0), shape)
    return out.resize((size, size), Image.LANCZOS) if SUPERSAMPLE > 1 else out


# ---------------------------------------------------------------- 输出
def main() -> None:
    root = pathlib.Path(__file__).resolve().parent.parent
    web_assets = root / "web" / "src" / "assets"
    web_public = root / "web" / "public"
    logo_dir = root / "packaging" / "logo"
    for d in (web_assets, web_public, logo_dir):
        d.mkdir(parents=True, exist_ok=True)

    svg = build_svg()

    # ---- 自校验：两道门禁，都是真踩过的坑，且都会让 logo「静默消失」（不报错）----
    #
    # 1) 必须是合法 XML。最阴的一条：XML 注释里**不能出现连续两个连字符**。
    #    把 CSS 变量名（以两个连字符开头）写进注释，就会让整个文档非法，
    #    浏览器直接拒绝渲染整个 SVG，且控制台不给任何提示。
    try:
        ElementTree.fromstring(svg)
    except ElementTree.ParseError as e:
        raise SystemExit(f"[logo] SVG 不是合法 XML：{e}") from e

    # 2) 必须是纯 ASCII。前端打包器会把小 SVG 内联成 data: URI，而非 ASCII 在该形态下
    #    需要百分号编码，工具链未必会做，浏览器同样可能拒绝渲染。
    non_ascii = sorted({c for c in svg if ord(c) > 127})
    if non_ascii:
        raise SystemExit(
            f"[logo] SVG 含非 ASCII 字符 {non_ascii}——内联后无法渲染。"
            "请把注释/文本改为英文（几何图形与色值不受影响）。"
        )

    (web_assets / "logo.svg").write_text(svg, encoding="utf-8")
    (web_public / "favicon.svg").write_text(svg, encoding="utf-8")
    print(f"  svg  → {web_assets / 'logo.svg'}")
    print(f"  svg  → {web_public / 'favicon.svg'}")

    # 位图：256 起足够生成 ICO 的全部尺寸，单独再出一张 1024 供展示
    for size in (1024, 256):
        out = logo_dir / f"videorag-{size}.png"
        build_rgba(size).save(out)
        print(f"  png  → {out}")

    ico_src = build_rgba(256)
    ico = logo_dir / "videorag.ico"
    ico_src.save(ico, sizes=[(s, s) for s in ICO_SIZES])
    print(f"  ico  → {ico}  ({', '.join(str(s) for s in ICO_SIZES)})")

    fav = web_public / "favicon.ico"
    ico_src.save(fav, sizes=[(s, s) for s in FAVICON_SIZES])
    print(f"  ico  → {fav}")

    print("\n完成。改设计只需调整本文件顶部的几何参数后重跑。")


if __name__ == "__main__":
    main()
