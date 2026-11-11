# 角色提示词目录

按`character_id`分类，`characters.card_path`保存对应目录，路径可以是绝对路径，或相对backend项目根目录的路径。

```text
resources/characters/
└── character_deepseek/                # 仓库自带的默认角色
    ├── 角色卡.md
    ├── 系统提示词.md
    ├── 语气提示词.md
    ├── manifest.json                 # 背景与立绘清单
    ├── background/                   # 背景图片
    ├── expression/                   # 立绘图片
    └── scenes/                       # 可选：场景召回对白数据
        └── sessions_train_by_expr/
```

三份提示词文件均使用 UTF-8。角色卡保留固定示例，系统提示词提供回复任务和输出要求，语气提示词作为独立资源加载。
新增角色时创建一个子目录，放入完整清单、提示词和图片，再启动后端完成登记。读取器通过 `characters.card_path` 定位资源目录。
三份提示词缺失或为空，以及目录中存在除这三份提示词、manifest.json 和许可/说明文件（`LICENSE*`、`NOTICE*`、`README*`）外的未知非隐藏文件，都会明确报错。资源子目录不参与提示词正文加载。

根目录的 `manifest.json` 是角色资源入口：背景及立绘文件路径均相对于角色根目录，默认背景是 `backgrounds.default_id` 对应的项目，立绘信息位于 `expression` 字段。
`expression.poses` 和 `expression.expressions` 的每项除中文名外还带一句使用说明（description），说明会渲染进角色卡。

**角色卡是模板，供模型使用时渲染**（系统/语气提示词原样读取）：
- 视觉表达小节里姿势和表情的允许列表夹在 Jinja2 注释标记（`poses:begin/end`、`expressions:begin/end`）之间，每次读取按 manifest 现值重新生成，只列启用且有图的资源；改清单请编辑 manifest，不要改卡片里的这两段。
- 称呼变量 `{{ user_name }}` 替换为 characters 表该角色的称呼；未声明的变量会让读取直接报错。示例里的全角 `｛｛user｝｝：` 是用户发言行标记，不参与替换。
- 固定示例的 JSON 字符串内使用 `{{ user_name | json_string }}`，保留 JSON 外层引号。普通正文继续使用 `{{ user_name }}`。
- 编辑接口读写源模板，保留称呼变量和清单标记；版本号仍由文件原始字节计算。

应用启动时扫描 `resources/characters/*/manifest.json`。每份清单通过 `character_id`、`name` 和 `user_appellation` 声明一个角色，资源目录取清单所在目录。角色 ID 必须唯一。数据库已有角色时保留已设置的名称、目录和称呼，并补齐空字段。
characters.character_setting不再作为本次加载正文的来源，角色文件才是提示词正文来源。

回复采用单阶段生成，使用系统提示词和渲染后的角色卡。角色清单中的可用姿势和表情随提示词冻结，生成后用这份快照校验。固定示例可保留在角色卡正文，也可展开为示范对话消息。`FrozenPrompt.tone_prompt` 继续保留语气提示词，供历史记录恢复和校验。

