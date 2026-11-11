"""记忆索引与召回共用的 BGE 编码技术适配，不承载业务查询或发布规则。"""
from module.shared.memory.types.encoding import DualEncoder
from pathlib import Path


class BgeMemoryEncoder:
    @staticmethod
    def encode_dual_vectors(texts: list[str], model: DualEncoder, batch_size: int = 16) -> list[dict]:
        """批量生成 Qdrant 可直接使用的双向量（dense + sparse）。

        功能：用 BGEM3FlagModel 一次批量编码全部文本，把每篇的 dense 向量和
        sparse 词权（lexical_weights）整理成 PointStruct / Prefetch 直接可用的
        形态——{"dense": list[float], "sparse": {"indices": list[int],
        "values": list[float]}}。文档向量与查询向量必须出自同一模型实例，
        本函数两侧共用（入库传全部匹配文本、查询传单条 query），在代码结构上
        保证这条红线。同步重计算，异步调用方须经 asyncio.to_thread 包装。

        输入：texts——匹配文本列表（查询侧则包一层 [query]）；model——
        load_encoder() 加载的 BGEM3FlagModel 实例，进程内复用；batch_size——
        一次前向的批大小，缺省 16。

        输出：与 texts 等长的列表，第 i 项对应 texts[i] 的双向量 dict，
        直接填进 models.PointStruct(vector=...) 或 Prefetch(query=...)。
        """
        out = model.encode(
            texts,
            batch_size=batch_size,
            return_dense=True,
            return_sparse=True,
            return_colbert_vecs=False,
        )
        # out 是 dict，keys = ['dense_vecs', 'lexical_weights']
        return [
            {
                "dense": dense.tolist(),
                "sparse": {
                    "indices": [int(token_id) for token_id in weights],
                    "values": [float(w) for w in weights],
                },
            }
            for dense, weights in zip(out["dense_vecs"], out["lexical_weights"])
        ]

    @staticmethod
    def load_encoder(model_path: str) -> DualEncoder:
        if not Path(model_path).is_dir():
            raise ValueError(
                f"本地记忆编码器目录不存在: {model_path}。"
                "请运行 backend/.venv/Scripts/python backend/scripts/download_models.py 下载,"
                "或在 .env 中配置 RNA_MEMORY_EMBEDDING_PATH 指向 BAAI/bge-m3 模型目录"
            )
        from FlagEmbedding import BGEM3FlagModel
        return BGEM3FlagModel(model_path, use_fp16=False)
