"""前后台共用的双向量编码契约。"""
from typing import Any, Protocol


class DualEncoder(Protocol):
    """bge-m3和测试替身共同提供的双向量编码接口。"""

    def encode(self, texts: list[str], batch_size: int = 16,
               **kwargs: Any) -> dict[str, Any]: ...
