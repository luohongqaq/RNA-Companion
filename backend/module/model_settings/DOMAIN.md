# 模型设置领域数据

| 数据 | 功能与位置 |
| --- | --- |
| [ModelPurpose](core/domain/models.py) | 区分 `reply` 和 `default` 两组用途。 |
| [ModelSettings](core/domain/models.py) | 保存展示用的地址、模型、密钥配置状态、有效值、覆盖字段、重启提示及文件版本。 |
| [ModelSettingsUpdate](core/domain/models.py) | 提交预期版本和要变更的字段；未提供的不修改，密钥为 `None` 表示清除。 |
| [ModelSettingsRepository](core/domain/ports.py) | 定义按用途查询和更新配置的能力，不暴露具体存储实现。 |

处理流程见 [ARCHITECTURE.md](ARCHITECTURE.md)。
