"""切换 embedding 模型后一键重建向量库（CLI 入口）。

核心逻辑在 app/core/embed/rebuild.py（Web「设置 → 本地模型 → 重建知识库」
按钮与 scripts CLI 共用同一实现，避免两处漂移）。本脚本仅做：
  1. 读 Settings（可 --provider/--model/--base-url 覆盖 embed 配置）
  2. 交互确认（--yes 跳过）→ 调 app.core.embed.rebuild.reembed

场景：更换 embedding 模型（如 bge-small-zh → bge-m3）后，向量库里的旧向量
与新模型不在同一语义空间——维度不同会直接报错，同维度不同模型则会静默失真
（模型指纹护栏会拦截并提示运行本脚本）。

原理：切片原文都保存在 SQLite chunks 表，无需重跑转写/笔记，只需：
  1. 读出全部 chunks（含 kind=meta 的简介分片，title/platform 从 meta JSON 恢复）
  2. 用新模型批量重嵌入
  3. drop LanceDB 表 → 按原 id 重建（lancedb_id 与 SQLite 保持一致，无需改库）
  4. 重建 FTS 索引 + 落盘新模型指纹（model.meta.json）

用法：
  .venv/bin/python scripts/reembed_vector_store.py            # 交互确认
  .venv/bin/python scripts/reembed_vector_store.py --yes      # 免确认（CI/脚本）
  可选：--provider openai --model bge-m3:latest --base-url http://host:11434/v1
"""
import argparse
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.config import Settings
from app.core.embed.embedder import Embedder
from app.core.embed.rebuild import load_rows, reembed  # noqa: F401（test_reembed_script 按名访问）
from app.core.vector_store import VectorStore


async def main() -> None:
    parser = argparse.ArgumentParser(description="重建 LanceDB 向量库（换 embedding 模型后）")
    parser.add_argument("--yes", action="store_true", help="跳过交互确认")
    parser.add_argument("--provider", default="", help="覆盖 embed provider（如 openai）")
    parser.add_argument("--model", default="", help="覆盖 embedding 模型名（如 bge-m3:latest）")
    parser.add_argument("--base-url", default="", help="覆盖远程 embedding 服务地址")
    args = parser.parse_args()

    settings = Settings()
    if args.provider:
        settings = settings.model_copy(update={"embed_provider": args.provider})
    if args.model:
        settings = settings.model_copy(update={"embed_model": args.model})
    if args.base_url:
        settings = settings.model_copy(update={"embed_base_url": args.base_url})

    if settings.embed_provider != "fastembed" and not settings.embed_base_url:
        sys.exit("错误：provider=openai 需要 --base-url 或在 .env 配置 EMBED_BASE_URL")

    rows = load_rows(settings.db_path)
    vs = VectorStore(settings.lancedb_path)
    old_meta = vs.get_model_meta()
    embedder = Embedder(
        model_name=settings.embed_model,
        provider=settings.embed_provider,
        base_url=settings.embed_base_url,
        api_key=settings.embed_api_key,
    )
    fp = embedder.fingerprint

    print(f"SQLite 切片数：{len(rows)}（db: {settings.db_path}）")
    print(f"旧模型指纹：{old_meta or '（无，首次建档）'}")
    print(f"新模型指纹：{fp}")
    if not args.yes:
        ans = input("确认 drop 向量库并重建？[y/N] ").strip().lower()
        if ans != "y":
            sys.exit("已取消。")

    def _progress(done: int, total: int) -> None:
        print(f"\r  嵌入进度：{done}/{total}", end="", flush=True)

    n = await reembed(rows, embedder, vs, progress=_progress)
    print(f"\n完成：重建 {n} 条切片 → {settings.lancedb_path}，指纹已落盘 model.meta.json。")


if __name__ == "__main__":
    asyncio.run(main())
