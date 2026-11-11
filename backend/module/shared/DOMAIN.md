# 共享领域数据

## 聊天上下文

| 类型与位置 | 作用 |
| --- | --- |
| [MessageListItem、CompletedTurnResult](context/types/messages.py) | 共享消息及完整对话轮的读取结果。 |
| [TurnRange、CurrentTurnRef](context/types/turns.py) | 固定历史轮次范围和当前输入引用。 |
| [SnapshotRef、InheritedContextRef、ContextSourceRef](context/types/references.py) | 摘要版本、继承材料和来源修订引用。 |
| [StructuredSummary、Commitment、SceneState](context/types/summary.py) | 结构化摘要及约定、场景等内容。 |
| [WorkingContext、InheritedContext、CurrentTurnInput](context/types/working.py) | 已加载的工作上下文、继承材料和当前输入。 |
| [VisualState、Pose、Expression](context/types/visual.py) | 姿势和表情状态。 |

## 记忆和任务

| 类型与位置 | 作用 |
| --- | --- |
| [MemoryRevisionRef、EvidenceRef、MemoryKind](memory/types/references.py) | 记忆版本、原始消息证据和记忆类别。 |
| [RecallTier、MemoryRetention](memory/types/retention.py) | 召回池及保留信息；不单独实现遗忘流程。 |
| [IndexHit、RepresentationKind](memory/types/indexing.py) | 索引命中和表示类别。 |
| [DualEncoder](memory/types/encoding.py) | 编码能力的业务端口。 |
| [调度数据类型](jobs/types/models.py) | 任务引用、优先级、调度决定和执行结果等共享类型；具体功能仍定义自己的任务状态。 |
| [RetryPolicy、WorkerPoolConfig](jobs/config/) | 重试和 Worker 配置。 |
| [业务异常](common/errors/base.py)、[分页](common/types/pagination.py)、[ID](common/ids.py) | 通用错误、列表返回和标识生成。 |

整体依赖关系见 [ARCHITECTURE.md](ARCHITECTURE.md)。
