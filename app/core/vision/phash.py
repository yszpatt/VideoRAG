"""图像指纹去重（E3）：dHash + 汉明距离。

用 Pillow 手写（约 30 行），不引入 imagehash 等第三方库：
- dHash（difference hash）：灰度缩放到 9×8，逐行比较相邻像素亮度，得到 64 bit；
- 相邻帧汉明距离 ≤ 阈值（默认 8）视为同一画面（同一 PPT 页/同一场景）。

Pillow 缺失或图片不可解码时返回 None，调用方跳过该帧的去重（不 fail 流程）。
`uv.lock` 中 Pillow 已作为 fastembed 传递依赖存在；本模块对它是软依赖。
"""

# 判定为「同一画面」的汉明距离上限（64 bit 指纹，经验值 8 左右）
DEFAULT_HAMMING_THRESHOLD = 8


def dhash(path: str, size: int = 8) -> int | None:
    """计算图片的 dHash 指纹（int）；失败返回 None。"""
    try:
        from PIL import Image
    except Exception:  # Pillow 未安装：跳过去重，不影响流程
        return None
    try:
        with Image.open(path) as im:
            resample = getattr(Image, "Resampling", Image).LANCZOS
            gray = im.convert("L").resize((size + 1, size), resample)
            px = gray.tobytes()  # L 模式：每像素 1 字节，避免 getdata 弃用告警
    except Exception:
        return None
    bits = 0
    width = size + 1
    for row in range(size):
        base = row * width
        for col in range(size):
            bits = (bits << 1) | (1 if px[base + col] > px[base + col + 1] else 0)
    return bits


def hamming(a: int, b: int) -> int:
    """两个指纹的汉明距离（不同 bit 数）。"""
    return (a ^ b).bit_count()


def is_similar(
    a: int | None, b: int | None, threshold: int = DEFAULT_HAMMING_THRESHOLD
) -> bool:
    """两个指纹是否近似（任一为 None 时无法判定，返回 False 表示「不视为重复」）。"""
    if a is None or b is None:
        return False
    return hamming(a, b) <= threshold
