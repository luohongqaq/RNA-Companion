# 功能模块

`module/` 与 `api/` 平级，按功能组织代码。各功能内部的 `core` 是业务层，
`repository` 是持久化和外部能力实现层；入口、路由和依赖装配属于 `api`。

当前已迁入 [memory_management](memory_management/README.md)、
[memory_recall](memory_recall/README.md)、[user_edit](user_edit/README.md)、
[context_compaction](context_compaction/README.md)、[chat](chat/README.md)、
[character](character/README.md) 和 [model_settings](model_settings/README.md)。
其他现有功能暂保留原目录。

```text
backend/
  api/
  module/
    memory_management/
      daily/
      object/
      topic/
      shared/
    memory_recall/
      agent/
      retrieval/
      cache/
      shared/
    user_edit/
      memory_edit/
    context_compaction/
      core/
      repository/
    chat/
      conversation/
      reply/
      scenes_recall/
    character/
      core/
      repository/
    model_settings/
      core/
      repository/
    shared/
      common/
      context/
      jobs/
      memory/
  storage/    # 跨模块共享的技术基础设施
```

跨功能共享的业务代码见 [shared](shared/README.md)。

开发文档：[模块架构总览](ARCHITECTURE.md) · [领域数据导航](DOMAIN.md)。
