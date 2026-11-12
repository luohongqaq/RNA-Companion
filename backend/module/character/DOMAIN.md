# 角色领域数据

## 角色和提示词

| 数据 | 功能与位置 |
| --- | --- |
| [CharacterInfo](core/domain/resources.py) | 角色 ID、名称、资源目录和对用户的称呼。 |
| [CharacterCard、CharacterReply](core/domain/models.py) | 角色卡正文、来源，以及角色对用户的称呼（user_appellation，供记忆总结替换称谓）；角色回复正文及视觉状态。 |
| [CharacterPrompt、PromptType](core/domain/prompts.py) | 一种提示词的正文、类型和版本；保存时版本用于并发检查。 |
| [CharacterCardDocument、CharacterSelectionView](../shared/character/document.py) | 从角色卡正文提取六节规则，提供六节标题的登记校验，记录正文的来源哈希，并带上角色对用户的称呼（记忆总结用作占位替换）。 |

## 背景和立绘

| 数据 | 功能与位置 |
| --- | --- |
| [CharacterResourceManifest](../shared/character/types/manifest.py) | 组合角色登记字段、背景目录和立绘目录。 |
| [BackgroundCatalog、BackgroundEntry](../shared/character/types/manifest.py) | 背景集合、默认项与图片路径。 |
| [ExpressionCatalog、ExpressionImage、ExpressionSelection](../shared/character/types/manifest.py) | 描述服装、姿势、表情、资源组合和默认选择。 |
| [ExpressionMatch](core/domain/resources.py) | 返回匹配的立绘资源及是否使用回退。 |
| [CharacterMetadataPort、CharacterResourcePort](core/domain/ports.py) | 定义元数据、清单、文件和提示词的读取与保存能力。 |

流程见 [ARCHITECTURE.md](ARCHITECTURE.md)。
