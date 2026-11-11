# 记忆查询与编辑架构

## 总览

```mermaid
flowchart LR
    A[用户 / API] --> Q[MemoryQueryService<br/>列表与详情]
    A --> E[MemoryEditService<br/>保存、查询与重试]
    Q --> DB[(SQLite 正式记忆)]
    E --> DB
    E --> J[(编辑快照与索引任务)]
    J --> W[Worker / Workflow]
    W --> I[MemoryEditIndexService]
    I --> V[(Qdrant 分区索引)]
```

API 路由见 [memories.py](../../../api/routes/memories.py) 和 [memory_edits.py](../../../api/routes/memory_edits.py)；服务与仓储由 [api/container.py](../../../api/container.py) 装配。`memory_edit` 业务定义端口，SQLite 和 Qdrant 实现在 `repository/`。

## 查询与保存

```mermaid
flowchart TD
    Q[列表 / 详情请求] --> V[校验角色、类别、分页游标]
    V --> R[读取正式版本并投影公开正文]
    E[提交完整正文、预期版本和幂等键] --> C[校验内容和引用关系]
    C --> O[读取当前正文<br/>保留未改动条目的原证据]
    O --> S[同一事务校验幂等与版本<br/>切换正文、保存编辑快照]
    S --> A[返回新版本与待索引状态]
    A --> N[通知后台，轮询兜底]
```

查询由 [MemoryQueryService](core/processing/query_service.py) 编排，分页游标由 [MemoryCursorCodec](core/processing/cursor.py) 编解码。[MemoryEditService](core/processing/edit_service.py) 先调用 [正文数据类](core/domain/document.py) 的 `validate()`，再读取旧版本，调用 `apply_to()` 重建正文并交给 [SQLite 仓储](repository/sqlite.py)。Daily 保留归属日期和来源会话。Topic 校验条目引用。只有未改动的正文沿用原证据。

## 后台重建索引

```mermaid
flowchart LR
    P[PENDING 编辑] --> C[领取固定版本和执行代次]
    C --> I[生成 Daily / Topic 索引表示]
    I --> Q[事务外写入 Qdrant]
    Q --> D[核对当前版本和代次后完成]
    Q -->|失败| R[有限退避或标记失败]
    R -->|手动重试| P
```

[MemoryEditWorker](core/scheduling/worker.py)、[MemoryEditWorkflow](core/scheduling/workflow.py) 与 [MemoryEditIndexService](core/processing/index_service.py) 推进任务；[Qdrant 实现](repository/qdrant.py)复用公共索引。正文先切版、索引后完成；失败不回退正文，新版本可使旧索引任务过时。
