# 回放评估

单测能保证不崩溃，保证不了"她还是她"。改一句提示词之后，输出会整体偏移，而这种偏移不会让任何断言失败。所以这个项目需要一把能读的尺子。

`replay_eval.py` 把一份真实历史对话（旧版桌宠导出的 `messages.xlsx`）按原始时间重放到当前系统，逐轮记录召回结果、真正注入提示词的记忆原文、以及模型生成的回复。

本机自用，产物不进版本库。

---

## 一次回放做了什么

1. **读表格**：按 session 分组，按 `created_at` 排序，得到段与轮。
2. **平移时间轴**：整份日志平移到最近，保持原始间隔与时分秒，最后一段落在昨天。平移基准取全部段的末尾，分批接力不会错位。
3. **逐段重放**：每段发消息 → 等回复 → 段末改写消息时间为虚拟时间 → 封存会话触发记忆生产 → 等记忆任务跑完。
4. **复用同一个对话**：封存会把当前会话换成后继会话，下一段接着用。这样上一段的交接摘要才会作为继承上文进入下一段的提示词——否则"上下文压缩"只测了生成、没测消费。
5. **按虚拟日期降冷**：每段开始前调一次 `POST /maintenance/retention`，把降池推进到该段的虚拟日期。衰减、冷热分层、记忆归属日期都和真实使用一致，而不是用真实"今天"去降冷历史数据。
6. **落盘**：每轮写完立刻 flush，中断不丢已完成的轮次。

## 运行

回放需要一个**独立的 data root**，不要指向日常使用的 `memory_record/database`：

```powershell
cd backend

# 1. 用独立数据根启动后端（Qdrant 本地模式独占目录，不能和日常实例共用）
$env:RNA_DATA_ROOT = 'C:\temp\replay'
$env:RNA_LOG_DATABASE_PATH = 'C:\temp\replay\logs.sqlite3'
$env:RNA_MEMORY_QDRANT_PATH = 'C:\temp\replay\memory_qdrant'
$env:RNA_MEMORY_PRODUCTION_ENABLED = 'true'
.venv\Scripts\python server.py

# 2. 另开一个终端跑回放（产物默认写到 backend\var\replay-out）
.venv\Scripts\python scripts\replay_eval.py `
  --xlsx <messages.xlsx> `
  --data-root C:\temp\replay `
  --api http://127.0.0.1:8765 `
  --output D:\RNA Companion\backend\var\replay-full
```

| 参数 | 说明 |
|---|---|
| `--limit N` | 只跑前 N 段；`0` 表示跑到结尾 |
| `--skip N` | 跳过前 N 段，长日志分批接力用 |
| `--resume` | 接着上次的产物继续写，对话稿不断档 |
| `--output` | 产物目录，默认 `backend/var/replay-out` |

三个注意点：

- **提示词改动要重启后端**。记忆生成、交接总结是模块级常量，进程启动时载入；角色卡、系统提示词是每次请求读文件，改完即时生效。两者行为不同，容易误判"改了没效果"。
- **每段封存后要等记忆生产完成**，最长等 180 秒，这段时间占了回放耗时的一半以上。
- **封存失败不能中断整轮**。摘要超预算时交接任务会判定失败、服务端返回 409；脚本捕获后换新对话继续，否则一次失败会丢掉后面所有段。

## 产物

| 文件 | 用途 |
|---|---|
| `replay.html` | 边跑边刷新的对话页；每条回复下面可展开**本轮真正注入的记忆原文**，附人称计数 |
| `replay_metrics.csv` | 每轮一行，便于直接统计 |
| `replay_turns.json` | 全量轮次，含召回审计（轨迹、耗时、source_refs） |
| `replay_transcript.md` | 纯文本对话稿，读起来最快 |
| `replay-conversation.txt` | 当前复用的对话 ID，分批接力时接着用 |

`replay_metrics.csv` 的列：

| 列 | 含义 |
|---|---|
| `seq` / `segment` / `at` | 轮次、所属段、虚拟时间 |
| `query` / `reply` | 用户输入与角色回复 |
| `recall_status` | `ready`（注入了记忆）/ `empty`（库空或判定无需历史） |
| `reason_code` | 召回结束原因，如 `selected` / `context_sufficient` |
| `recall_seconds` | 端到端召回耗时（含 Agent 决策） |
| `selected` | 选中的记忆文档数 |
| `mem_chars` | 注入的记忆原文长度 |
| `mem_user` / `mem_ta` / `mem_chief` | 记忆原文里"用户""他""主人"各出现多少次 |

最后三列是为**人设漂移**专门加的：不用翻数据库，直接看记忆里用的什么称谓。

## 

## 局限

- **同一份输入两次回放，输出不完全相同**（回复是现生成的）。适合做 A/B 对比，不适合当固定基准。
- **没有人工标注的正确答案集**，所以不报准确率。
- **语义质量只能人读。**"同一天的几条记忆该不该合并""总结有没有漏掉关键信息""一次性行为有没有被记成约定"，这些规则筛不准——用关键词判断"否定是否保留"，误报接近 100%。
- **时间轴只对记忆侧和封存侧生效。** 回放改写消息时间是在段末做的，而召回与总结发生在对话过程中，读到的是消息入库时的真实时间——它们从 `CHAT_CONTEXT.created_at` 推断"今天"。所以记忆文档的 `memory_date` / `acquire_time` 是虚拟的，召回总结正文里的日期是真实的；交接摘要在改写之后运行，是虚拟的。按日期做统计分析时不能混用。