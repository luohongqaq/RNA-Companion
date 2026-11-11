# 后端开发规范

本规范适用于 `backend/`。修改后端前先阅读本文和目标模块的开发文档。后端代码默认使用 Python。

## 分层与依赖

- 运行时代码分为 API、业务和存储三层。功能目录里的 `core/domain` 属于流程实体， `core/processing` 属于流程处理层，`repository` 与顶层 `storage/` 属于存储实现层。
- `api/` 负责 HTTP 协议、参数转换、依赖装配和应用生命周期。业务规则归功能模块。
- `core/domain` 定义领域数据、状态、业务端口，以及依赖这些数据的校验和计算规则。保持领域方法可用明确输入独立执行。
- `core/processing` 负责用例和流程编排，通过业务端口访问数据库、模型或文件。可复用的一组业务行为由职责明确的类承接，由服务组合调用。
- `repository` 和 `storage/` 实现业务端口，负责数据库、文件、索引、模型 SDK 和持久化映射。业务层禁止导入 FastAPI、SQLite 驱动、Qdrant 或具体 LLM SDK。
- 跨功能共用的类型和规则放在 `module/shared/`。共享代码只依赖共享代码和通用库，具体功能模块通过组合复用。

## 函数和类的归属

- 仓储映射函数可以按职责集中在 `mappers.py`、`document_mappers.py` 或 `job_fields.py`。
- 先找已有的领域类或服务类。校验、投影、计算等领域行为归到拥有相应数据的类。任务创建用对应任务输入类的 `create_command()`，目标键和幂等键由这一处定义。
- 流程入口和步骤归到对应的服务类。多步骤流程由一个入口统一管理执行顺序、截止时间和取消。单次执行的可变状态在每次调用时创建，避免并发调用互相污染。
- 无状态操作可用静态方法。通过类构造领域对象或启动一次独立执行时可用类方法。只有可区分的职责、依赖或状态才值得拆出新类。类名应说明处理的对象和职责。
- JSON 编解码、哈希和 ID 生成复用已有类。`SQLiteValueCodec` 负责 SQLite JSON 格式，`ReplyFingerprint` 负责回复相关指纹，`CanonicalJsonFingerprint` 和 `IdGenerator` 提供通用能力。

## 时间、模型与配置

- 时间戳存储和传输使用带时区的 UTC。日记归属、留存日期和按日召回使用运行后端的电脑时区。统一使用 `BackendTime.utc()` 与 `BackendTime.local()`。按日查询分别计算当天和次日的当地零点，再用 UTC 比较区间。
- 已登记任务恢复时沿用固定的业务日期和来源范围。已冻结的模型输入、资源选项、来源引用和哈希应支持重试与重启恢复。
- API 容器通过 `ModelClientFactory` 按用途获取模型客户端。模型名、工具 schema、输出格式和调用参数由对应适配器随请求传入。停止使用客户端的后台任务后，再关闭共享连接。
- 同一规则只维护一份。角色视觉清单和回复视觉校验共用 manifest 派生的选项。召回工具的输入模型由工具注册表提供。

## 修改与验证

- 通过真实入口验证改动。重构时保留接口行为、存储字段、哈希格式、幂等键和恢复流程。涉及保存、重试、并发或旧数据时，验证重复调用和失败恢复。
- 先跑相关测试，再按影响面跑架构与集成测试。使用项目的 Python 环境，例如 `./.venv/Scripts/python.exe -m pytest -q tests/test_architecture.py`。
- 移动类、方法或职责时同步更新相关模块的 `ARCHITECTURE.md`、`DOMAIN.md` 和已有的类与方法清单。流程文档说明实际调用顺序，代码索引链接到当前实现。

## 参考实现

- 角色卡渲染、段落提取与启动登记见 [角色模块架构](module/character/ARCHITECTURE.md)。
- 任务构造、时间线处理与留存见 [记忆管理架构](module/memory_management/ARCHITECTURE.md)。
- 领域、编排、读取和持久化职责见 [召回架构](module/memory_recall/ARCHITECTURE.md) 和 [召回类与方法清单](module/memory_recall/CODE_MAP.md)。
- 存储约定见 [SQLite 存储说明](storage/sqlite/README.md)。
