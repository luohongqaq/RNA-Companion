# 共享业务能力架构

共享模块提供供其他功能复用的业务契约和通用规则；不装配应用，也不实现 SQLite、模型或向量数据库。技术基础设施位于顶层 `storage/`。

## 总览

```mermaid
flowchart TD
    C[chat / context_compaction] --> CT[shared/context]
    M[memory_management / memory_recall / user_edit] --> MT[shared/memory]
    C --> J[shared/jobs]
    M --> J
    C --> G[shared/common]
    M --> G
```

## 后台 Worker

```mermaid
flowchart LR
    S[start] --> R[子类 recover]
    R --> P[process_next 串行推进]
    P -->|有进展| P
    P -->|无进展| W[等待 notify 或轮询超时]
    W --> P
    P --> X[stop 取消并等待]
```

[Worker](jobs/worker.py) 管理生命周期和唤醒，具体任务由各功能实现；[RetryPolicy](jobs/config/retry.py)提供退避配置，不代替业务 Workflow 的错误分类。共享数据位置见 [DOMAIN.md](DOMAIN.md)。
