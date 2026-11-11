"""索引编码阶段的向量值和可恢复产物。

SparseVector 与 EmbeddingBatch 表达按输入顺序对应的稠密、稀疏向量及模型版本。
EmbeddingArtifact 保存外部编码产物的路径、哈希、数量和来源表示哈希，本文件只定义数据，不执行文件读写。
"""
from __future__ import annotations
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class SparseVector:
    indices: tuple[int, ...]
    values: tuple[float, ...]


@dataclass(frozen=True, slots=True)
class EmbeddingBatch:
    """dense/sparse与输入表示顺序一一对应；大向量保存为artifact。"""
    dense: tuple[tuple[float, ...], ...]
    sparse: tuple[SparseVector, ...]
    model_version: str


@dataclass(frozen=True, slots=True)
class EmbeddingArtifact:
    path: str
    content_hash: str
    vector_count: int
    model_version: str
    source_representations_hash: str
