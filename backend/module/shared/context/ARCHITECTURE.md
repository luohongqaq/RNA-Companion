# 共享上下文架构

```mermaid
flowchart LR
    A[聊天 / 压缩固定引用] --> R[轮次、摘要和继承引用]
    R --> L[调用方读取对应正文]
    L --> W[WorkingContext<br/>摘要、完整轮、当前输入]
    W --> P[回复提示词 / 记忆选择]
```

[references.py](types/references.py)、[turns.py](types/turns.py)描述引用；[working.py](types/working.py)组合已读取材料；[summary.py](types/summary.py)描述和渲染结构化摘要。实际读取与交接分别属于聊天及上下文压缩模块。领域数据见 [DOMAIN.md](DOMAIN.md)。
