import json
from pathlib import Path

import lancedb
import pyarrow as pa

# 行类型：content=口播切片（带时间戳）| meta=视频级简介分片（无时间戳）
KIND_CONTENT = "content"
KIND_META = "meta"


class VectorStoreIncompatible(ValueError):
    """向量库与当前 embedding 模型不兼容（维度不匹配 / 模型指纹已切换）。

    单独成类而非裸 ValueError：检索/问答端点据此返回 409 + 可读 detail
    （见 main.py 异常处理器），而不是被 FastAPI 吞成裸 500 Internal Server
    Error，用户才能看到「需要重建知识库」的具体指引。
    """


class VectorStore:
    """LanceDB 向量库（文件型，单容器内嵌）。懒连接：构造不触达磁盘。"""

    def __init__(self, path: str, table_name: str = "chunks"):
        self._path = path
        self._table_name = table_name
        self._db = None

    def _get_db(self) -> lancedb.DBConnection:
        if self._db is None:
            self._db = lancedb.connect(self._path)
        return self._db

    @staticmethod
    def _quote(v: str) -> str:
        """SQL where 片段里的单引号转义。"""
        return v.replace("'", "''")

    def _embedding_dim(self, tbl) -> int | None:
        """表的 embedding 列维度（fixed_size_list）；无该列返回 None。"""
        try:
            f = tbl.schema.field("embedding")
            return f.type.list_size
        except (KeyError, AttributeError):
            return None

    def _check_dim(self, tbl, vec_len: int, action: str) -> None:
        """维度校验：切换 embedding 模型后新旧维度不一致时给出明确报错。

        不校验时 LanceDB 会抛 "There is no vector column" 这类误导性错误。
        """
        dim = self._embedding_dim(tbl)
        if dim is not None and vec_len != dim:
            raise VectorStoreIncompatible(
                f"向量维度不匹配（{action}）：当前 embedding 输出 {vec_len} 维，"
                f"但向量库中已有数据为 {dim} 维。更换 embedding 模型后需重建"
                f"知识库（见下方指引），或改回原 embedding 配置。"
            )

    # ---- 模型指纹（旁车文件 model.meta.json，防「同维度不同模型」静默失真）----

    @property
    def _model_meta_path(self):
        return Path(self._path) / "model.meta.json"

    def get_model_meta(self) -> dict | None:
        """读取已记录的 embedding 模型指纹；未记录返回 None。"""
        p = self._model_meta_path
        if p is None or not p.is_file():
            return None
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None

    def set_model_meta(self, fingerprint: dict) -> None:
        """写入/覆盖模型指纹。"""
        p = self._model_meta_path
        if p is None:
            return
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(
            json.dumps(fingerprint, ensure_ascii=False, indent=2), encoding="utf-8"
        )

    def check_model_compat(self, fingerprint: dict) -> None:
        """校验当前 embedding 模型与建库模型是否一致；不一致抛明确 ValueError。

        - 首次校验（旁车文件不存在）时把当前指纹写入（老库平滑 adopting）；
        - model 或 dim 不一致都算切换——同维度不同模型时向量空间语义失真，
          比维度不匹配更危险（无报错、检索静默变差）。
        """
        existing = self.get_model_meta()
        if existing is None:
            self.set_model_meta(dict(fingerprint))
            return
        old_model = existing.get("model")
        new_model = fingerprint.get("model")
        old_dim = existing.get("dim")
        new_dim = fingerprint.get("dim")
        same = old_model == new_model and (
            old_dim is None or new_dim is None or old_dim == new_dim
        )
        if not same:
            raise VectorStoreIncompatible(
                f"embedding 模型已切换：向量库由 {old_model}"
                f"{'（%s 维）' % old_dim if old_dim else ''} 构建，"
                f"当前为 {new_model}{'（%s 维）' % new_dim if new_dim else ''}。"
                f"请在「设置 → 本地模型」点击「重建知识库」以当前模型重嵌入全部切片"
                f"（原文保留在 SQLite chunks 表，无需重跑转写；CLI 可用 "
                f"scripts/reembed_vector_store.py），或改回原 embedding 配置。"
            )

    def add(self, rows: list[dict]) -> None:
        """首次建表，之后追加。rows 必须含 id/embedding 等一致 schema。

        kind 列自适应：老表（无 kind 列）遇到带 kind 的新行时先整表迁移
        （to_arrow 重建补 content 列，保留既有向量与 id），再追加。
        FTS 索引随表重建而丢失，由调用方在写入后 ensure_fts_index 重建。
        """
        db = self._get_db()
        if self._table_name in db.table_names():
            tbl = db.open_table(self._table_name)
            has_kind = "kind" in tbl.schema.names
            if not has_kind and any("kind" in r for r in rows):
                self._migrate_kind(db)
            # 追加前校验维度，避免把不同模型的向量混入库
            first_vec = next(
                (r.get("embedding") for r in rows if r.get("embedding") is not None),
                None,
            )
            if first_vec is not None:
                self._check_dim(
                    db.open_table(self._table_name), len(first_vec), "写入"
                )
            db.open_table(self._table_name).add(rows)
        else:
            db.create_table(self._table_name, data=rows)

    def _migrate_kind(self, db: lancedb.DBConnection) -> None:
        """给存量表补 kind 列（重建表；embedding 列类型原样保留）。"""
        tbl = db.open_table(self._table_name)
        arrow = tbl.to_arrow()
        arrow = arrow.append_column(
            "kind", pa.array([KIND_CONTENT] * arrow.num_rows, type=pa.string())
        )
        db.drop_table(self._table_name)
        db.create_table(self._table_name, data=arrow)

    def ensure_kind(self) -> bool:
        """幂等迁移：表已含 kind 列或无表时返回 False；本次实际重建返回 True。"""
        db = self._get_db()
        if self._table_name not in db.table_names():
            return False
        if "kind" in db.open_table(self._table_name).schema.names:
            return False
        self._migrate_kind(db)
        return True

    def count_rows(self, where: str | None = None) -> int:
        """返回表行数（可带 SQL where 过滤）；表不存在/异常时返回 0。"""
        db = self._get_db()
        if self._table_name not in db.table_names():
            return 0
        try:
            return db.open_table(self._table_name).count_rows(where)
        except Exception:
            return 0

    def has_kind_rows(self, video_id: str, kind: str = KIND_META) -> bool:
        """该视频是否已有指定 kind 的行（用于幂等/backfill 去重）。"""
        vid = self._quote(video_id)
        return self.count_rows(f"kind = '{kind}' AND video_id = '{vid}'") > 0

    def search(
        self,
        query_vec: list[float],
        top_k: int = 5,
        where: str | None = None,
    ) -> list[dict]:
        db = self._get_db()
        if self._table_name not in db.table_names():
            return []
        tbl = db.open_table(self._table_name)
        self._check_dim(tbl, len(query_vec), "检索")
        q = tbl.search(query_vec).limit(top_k)
        if where:
            q = q.where(where)
        return q.to_list()

    def drop_table(self) -> None:
        """删除整个向量表（重建向量库用；表不存在时静默跳过）。"""
        db = self._get_db()
        if self._table_name in db.table_names():
            db.drop_table(self._table_name)

    def ensure_fts_index(self) -> None:
        """为 content 列建全文索引（ngram 2-gram，兼容中文）。"""
        db = self._get_db()
        if self._table_name not in db.table_names():
            return
        try:
            from lancedb.index import FTS

            db.open_table(self._table_name).create_index(
                "content",
                config=FTS(
                    base_tokenizer="ngram",
                    ngram_min_length=2,
                    ngram_max_length=2,
                ),
            )
        except Exception:
            pass  # 不支持/已存在时静默，检索侧会自动降级

    def search_text(
        self,
        text: str,
        top_k: int = 5,
        where: str | None = None,
    ) -> list[dict]:
        db = self._get_db()
        if self._table_name not in db.table_names():
            return []
        tbl = db.open_table(self._table_name)
        q = tbl.search(text, query_type="fts").limit(top_k)
        if where:
            q = q.where(where)
        return q.to_list()

    def delete_by_video_id(self, video_id: str) -> None:
        """删除某视频的全部向量分片（表不存在则静默跳过）。video_id 做单引号转义。"""
        db = self._get_db()
        if self._table_name not in db.table_names():
            return
        tbl = db.open_table(self._table_name)
        vid = video_id.replace("'", "''")
        try:
            tbl.delete(f"video_id = '{vid}'")
        except Exception:
            pass  # 向量删除失败不应阻断视频记录本身的删除
