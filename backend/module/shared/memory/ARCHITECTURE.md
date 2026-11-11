# 共享记忆契约架构

```mermaid
flowchart LR
    S[消息来源] --> E[EvidenceRef]
    D[正式记忆版本] --> R[MemoryRevisionRef]
    D --> T[RecallTier / MemoryRetention]
    Q[检索实现] --> H[IndexHit]
    I[索引和检索实现] --> P[DualEncoder 端口]
    E --> U[生产 / 编辑 / 召回]
    R --> U
    H --> U
```

[references.py](types/references.py)定义来源与版本引用，[retention.py](types/retention.py)定义保留信息，[indexing.py](types/indexing.py)定义索引命中，[encoding.py](types/encoding.py)声明编码能力。具体正文、持久化和索引流程属于其他功能模块。领域数据见 [DOMAIN.md](DOMAIN.md)。
