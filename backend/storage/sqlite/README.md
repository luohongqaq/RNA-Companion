# SQLite 存储适配器

使用 SQLAlchemy 2.0 异步 ORM 和 aiosqlite。业务层继续使用 dataclass 和 Protocol；
SQLAlchemy 对象只在 storage 内使用。

- `models.py`：应用共用的 SQLite 表模型。
- 角色、聊天、上下文压缩和记忆的业务映射位于各自模块的 `repository`。
  DailyJob/TopicJob通过memory_job_to_row、memory_job_to_domain共用任务表；读取时按job_family还原具体类型。
  调度字段使用独立列，schema_version=6的payload按daily/topic分别编码，只保存本类输入与阶段产物。
  daily_job_payload_to_json/topic_job_payload_to_json及各自input映射不混放另一类的字段。
  DailyMemory通过daily_memory_to_row和daily_memory_to_domain映射；角色、创建时间、召回池由存储调用方提供。
- [`SQLiteValueCodec`](value_codecs.py) 提供时间、轮次范围、上下文引用和记忆版本引用的 JSON 转换。时间方法通过 `BackendTime.utc()` 读写 UTC 时间戳，也可用作 `json.dumps()` 的 `default` 回调。
- 会话和回复任务的映射分别位于 [chat/conversation](../../module/chat/conversation/repository/mappers.py) 和 [chat/reply](../../module/chat/reply/repository/mappers.py)。
- 上下文快照和压缩任务的映射位于 [context_compaction](../../module/context_compaction/repository/mappers.py)。
- Daily、Object、Topic 的任务映射在各自仓储中，[共享任务映射](../../module/memory_management/shared/jobs/repository/mappers.py) 负责按任务类型分派，[正式文档映射](../../module/memory_management/shared/repository/document_mappers.py) 由记忆管理、召回和编辑共用。
- `db_session.py`：Engine、Session 工厂、读取 Session 和写事务。
- [`UTCDateTime`](types.py) 通过 `BackendTime.utc()` 将带时区时间存为 UTC ISO 文本，读取时返回带 UTC 时区的时间对象。业务日期的换算由调用方在应用层完成。

## 使用入口

当前基线包含 `chat_events`，持久保存 reply_committed 事件，事件 ID
使用 AUTOINCREMENT，防止删除后重用序号；同一正式消息只能登记一次该事件。
迁移不为旧消息补发事件。T3 accept_reply() 已在同一事务中写入 reply_committed
及完整正式回复快照；重复采用不重复写事件，任一步失败时整体回滚。

`SQLiteChatRepository.list_events_after(event_id, limit=100)` 读取严格大于游标的事件，
按事件序号升序返回 ChatEvent 元组（ReplyCommittedEvent 或 GenerationFailedEvent），limit 范围为 1～100。
游标为 0 时从头读取；锚点不存在或已删除不影响继续查询，没有后续事件返回空元组。
回复从事件的 reply_json 快照还原；快照标识不一致、时间缺时区或结构损坏时明确报错，
不会静默跳过事件或以当前消息覆盖历史快照。

`module/chat/conversation/core/processing/events.py` 的 ChatEventService 在仓储之上提供持续订阅：
默认每批 100 条、空闲每秒查询一次、空闲 15 秒交付 None 心跳标记。
每个订阅独立推进游标，有积压时连续读取；取消直接传播，仓储异常不会被当作心跳吞掉。
GET /api/v1/events 已接入订阅服务，API 把事件编码为 text/event-stream、把 None 编码为注释心跳。

`migrations/baseline.sql` 是版本 7 的完整建库基线，包括表、索引、触发器和初始更新时钟。
新库通过 initialize() 自动创建；本地旧开发库已备份并完成一次性数据迁移。
不再维护重复的 schema.sql，不能在非空库直接重放建库 SQL。

sessions 的 `session_seq` 从 1 开始，在同一 conversation 内唯一。
初始 session 使用 1，交接和种子导入在写事务内取当前最大序号加 1。
历史消息按 session_seq、turn_seq、message_seq 排序，不再在查询时递归计算顺序。

应用启动时调用 `await database.initialize()`；停止工作任务后调用
`await database.close()`。连接创建时启用外键、WAL、FULL 同步和 busy timeout。
Engine 和 `db_session_factory` 在同一个数据库管理器中复用，每次操作创建独立 Session。
同一管理器应在同一个事件循环内使用。

读取操作：

```python
async with database.db_session() as db_session:
    row = await db_session.get(MessageRow, message_id)
    result = message_to_list_item(row) if row is not None else None
```

写事务：

```python
async with database.transaction() as db_session:
    db_session.add(row)
    await db_session.flush()
# 到这里才代表提交成功；随后再返回保存结果。
```

每个并发任务使用自己的 `db_session`，事务内的辅助方法共用该 Session，不能自行
commit、rollback，也不能嵌套 `database.transaction()`。写事务的串行与优先级由外部
写队列和唯一 writer 负责，Session 管理器不加本地锁；写事务在首次访问数据库前发出
`BEGIN IMMEDIATE`。普通读取发出 `BEGIN DEFERRED`；
退出读取上下文不会自动提交。不要在数据库事务内等待模型或其他网络请求。

ORM 未定义 relationship，避免异步懒加载和隐式查询。由于路由有循环外键，创建时需要
显式 flush：角色 → current_session_id 为空的 conversation → session → 回填指针
→ turn → message → job → receipt；这些 flush 都属于同一事务。

## 映射约定

`*_to_row` 创建新 ORM 对象，不执行状态迁移或数据库操作。更新已有记录时，修改查询到的
ORM 对象，避免 merge 新副本覆盖 row_version、token 计数等业务实体未承载的字段。
conversation 映射需要显式传入 updated_at；receipt 映射需要显式传入 created_at。
Message 当前业务类型仍使用 action；visual 和 client_created_at 通过 mapper 的关键字参数
单独传入，不把动作字符串猜测为视觉状态。FrozenPrompt 的 hash 由生成用例提供，mapper
不另行发明计算规则。

JSON 字段由各映射文件按具体类型逐字段转换；嵌套值使用对应类型的小函数，
枚举显式读取 value，时间显式转 UTC ISO 字符串，tuple 显式转为 JSON 数组并在读取时还原。
不再使用通用反射转换器。

只使用当前 JSON 结构：继承摘要只保存 `summary_snapshot_ref`，其中包含 ID、hash
和覆盖点，不再单独保存重复的摘要 ID。字段允许显式为 null 或空数组，但缺少字段会报错，
不为旧材料补默认值。错误只保存于 `failure_json`，候选正文和 hash 保存于 `candidate_json`。

## 工作上下文读取

`SQLiteContextReadRepository` 的每次读取使用独立短读事务，不持有写锁，不执行生成资格检查。
每个session只有一份摘要，按固定ID、hash和覆盖点读取，只允许读取VALID状态；结构化摘要的全部
分区由 mapper 显式还原，缺字段不会补默认值。读取时不计算或校验正文 hash，来源 hash 仍随引用返回。
完整轮只返回用户输入和 accepted_message_id
指向的正式角色回复；未完成轮、缺失回复或错误归属会明确报错。

批量查询按 400 个参数分块，结果保持调用方指定顺序；缺少任何请求材料都不能静默忽略。
读取器返回纯业务类型，不把 ORM 对象或数据库 Session 传出 storage。加载器没有材料缓存，
已加载对象由调用方持有和复用。


## 迁移

空库由 `initialize()` 执行基线，并登记版本 1 至 6。已有库从 `migrations/legacy/` 按版本补齐缺少的历史迁移。后续增量脚本放在 `migrations/` 根目录，从 007 开始。结构变更和版本记录在同一事务提交。
已包含唯一session摘要、来源审计、turns.content_revision、等待交接、固定继承引用、daily登记标记及按日期建档的daily结构。
旧开发库与原迁移文件先归档备份后重建；业务主库统一为memory_record/database/rna_companion.db。

## 单协程上下文维护

已实现 create_job、get_job 和 get_next_ready_job。整个进程只允许一个上下文维护协程，
它必须 await 当前任务阶段处理结束后，才能读取下一个任务，不并发创建处理子任务。
get_next_ready_job 是只读调度查询：同 session/同类型遵守非终态队首规则，退避任务不能
被同组后续任务越过；不同目标按 priority、created_at、job_id 选出一个到期任务。
读取不修改状态，重复查询会返回同一未推进任务，这是单协程模型下的正常行为。

不再使用 claim_job、release_job 或 claim_token。完整 payload 写入版本 3，保留版本 1、
版本 2 和 T3 最小交接 payload 的读取；旧版本 2 的占用 token 明确忽略，保存时不再写入。
不修改 SQL 表结构或迁移文件。模型 attempt_version、来源版本及发布时的短事务校验仍保留，
因为前台输入可以并发变化，超时模型也可能迟到返回。

生成、审核、来源冻结及发布阶段仍保留脚手架；仓储尚未接入后台 worker。
启动恢复后续只需处理未交付模型尝试的代次，不再需要清理任务占用。
下列内容是后续实现约定，不表示已实现：

- 阶段为 need_generate → need_precheck → need_review → need_validate → need_publish → done。
- 阶段产物绑定候选 hash、版本和尝试代次，拒绝迟到结果覆盖新状态。
- 发布只保存 READY；在前台固定上下文的同一事务中校验并采用，替换 VALID。
- 来源修订或删除需使相关产物失效；范围外新增完整轮不应使固定来源作废。
- 重试和启动恢复保留已提交产物，避免重复调用模型或重复发布。
- prepare_handoff 候选不通过在线摘要采用入口激活，切段事务后续另行实现。

attempt 的 `input_boundary_json` 和 `updated_at` 必须非空。角色转换为完整角色卡时必须有
card_path，缺失材料时明确报错。单进程调度不保存租约字段，也不保留旧的非结构化错误列
及重复候选正文列。

T1 已支持首次发送及幂等重试：角色须预先存在，同一事务创建 conversation、session、turn、
用户消息、need_prepare 任务和回执。相同 request_id 和相同请求返回原结果；请求内容变化则
报幂等冲突。已有 conversation 的新请求暂时拒绝，追加消息、历史查询和有容量限制的写队列
仍待实现。事务外不调用模型，提交成功后才返回保存结果。


## 记忆总结与审查材料分开保存

frozen_prompt_json只保存最终模型输入、记忆总结及hash，不包含原卡片或检索轨迹。
原始卡片、来源版本及轨迹使用独立memory_audit_json列，由MemoryAudit进行显式JSON映射。
GenerationFreezeTicket携带frozen_prompt与memory_audit，T2-B在同一事务提交二者。

建库基线包含可空的 `memory_audit_json` 审查列；T2-B 已实现资格复查与冻结提交。
实际回复模型只读取FrozenPrompt.messages，审查列不参与模型消息拼接。


## T2-B、候选持久化与T3

SQLiteGenerationRepository.freeze_prompt校验job/attempt/turn关系、输入版本、active_token、
会话状态及当前用户消息顺序，并原子保存summary、提示词、清单、hash和独立审查证据。
成功推进need_generate；重复冻结返回False，不覆盖已冻结材料。

save_candidate核对完整候选hash与冻结prompt_hash，保存candidate_json并推进need_accept。
accept_reply仅采用持久化候选，保存正文及visual、封口turn、推进attempt/job终态、累计
本轮原文token。相同采用票据重试返回同一正式消息，不重复累计。
达到session准备阈值时在同一事务登记唯一prepare_handoff任务；长期记忆生产仍由session封存流程触发。
所有写入口使用BEGIN IMMEDIATE；数据库异常传播并整体回滚。不会检查后台记忆是否已切新版。

GenerationCompletionService串起已冻结材料→模型→候选提交→T3，也支持已有候选的恢复。
正式任务执行器和HTTP状态查询已接入，自动生成使用本地TokenCounter；前端可轮询状态，尚未提供事件推送。

同一事件表支持 generation_failed，使用独立失败快照并保留自增序号高水位。
`record_failure()` 在任务最终进入 failed 时，同事务写入 generation_failed；
自动重试期间不写最终失败事件，过期/重复登记不写事件。
失败事件保存 conversation_id、turn_id、input_version、manual_retry_count 和 error 快照，
同一个 job 的每一手动重试代次最多登记一次最终失败。迁移不补发历史失败。
API 使用同一 GET /api/v1/events 输出两种事件，Last-Event-ID / after 对两种事件统一排序补发。
客户端按 event_id 去重；manual_retry_count 为 0 表示初次执行，之后每次手动重试加 1。
error.retryable 表示错误本身是否适合重试，不表示后台仍将自动重试，手动重试资格由现有接口校验。

同一事件表通过 cancel_json 保存 generation_cancelled 取消快照，保留历史成功/失败快照及自增序号。
SQLiteGenerationRepository.cancel_job() 原子检查当前输入和可取消状态，将 job 标为 cancelled，将已有当前 attempt 标为 superseded，并写入取消事件。
不删除用户消息，不完成当前 turn，不修改其他输入的 active_token。重复取消幂等；已完成/失败/过期任务拒绝。
复用已有 _eligible 检查拒绝迟到候选、T3 和失败登记，不中断已发出的模型 HTTP 请求。

当前表和列使用 memory_objects/object_id/object_state，ORM 为 MemoryObjectRow。
mapper 直接读写当前 JSON 格式；topic 使用 entries/generation_result/result_version，object 使用 objects/object_id。
当前版本从基线创建新库，历史备份通过 `migrations/legacy/` 中的迁移升级。

当前记忆发布仅依赖结构化JSON：DailyWrite/TopicWrite保存document及content_hash，不持久化整篇渲染文本。render_text仅按需生成索引或召回文本，不写文件。
daily在一个SQLite事务内切换正式版本并提交DONE；topic保留正式版及object关联提交、Qdrant元数据同步、最终DONE的恢复流程。
002_json_memory_publication.sql移除正式表markdown_path，清理旧任务中的Markdown副本与导出产物记录，并将memory_jobs的payload升级为schema_version=5。

意向命名统一为intentions、DailyIntention、IntentionOwner/IntentionCertainty/IntentionStatus；查询正文为DailyIntentionContent。003数据迁移只转换结构字段，保留正文字符串，并将memory_jobs JSON升级为schema_version=6。
