# 后端日志

运行 `python server.py` 自动启用，默认写入 backend 根目录下的
`memory_record/database/logs.sqlite3`，与聊天数据库分开。SQLite 单独线程批量写入，
正常关闭先停止后台任务，再排空日志队列。

覆盖 HTTP 请求、用户消息提交、会话修改、生成准备/模型调用/候选保存/正式采用、
手动重试，以及 daily/object/topic 记忆生产、上下文维护和 Worker 生命周期。
这里的前台指前台聊天请求链路；尚未开发的前端客户端不会自动产生日志。

配置：`RNA_LOG_DATABASE_PATH`、`RNA_LOG_LEVEL`（默认 INFO）、
`RNA_LOG_RETENTION_DAYS`（默认 30，启动时清理到期记录）。
时间带 `+08:00` 时区；`file_path`、`line_number`、`function_name` 定位源码。
阶段装饰器标记函数入口位置，函数内部日志记录实际调用行。

业务代码使用标准 logging，或统一入口：

```python
from logger import get_logger, log_context, stage

log = get_logger(__name__)

@stage("example.process")
async def process(job_id: str):
    log.info("开始处理", extra={"event": "processing"})
```

`stage` 自动记录 started/finished/failed/cancelled 和毫秒耗时，并传递任务标识。
`stage_finished` 表示函数正常返回；不等于回复正式采用，后者看 `reply_committed`。
`request_id` 对应响应头 `X-Request-ID`；用户消息幂等 ID 存于
`context_json.message_request_id`，后台流程通过 `job_id`、`attempt_id` 串联。
不记录请求体、查询字符串、完整提示词、回复正文或模型密钥。
异常记录类型与源码调用栈，不保存异常消息和局部变量。
自定义日志请同样避免将正文、密钥写入 message。

使用 SQLite 工具打开日志库，执行：

```sql
SELECT timestamp, level, file_path, line_number, stage, event,
       job_id, attempt_id, duration_ms, message, exception
FROM logs ORDER BY id DESC LIMIT 100;

SELECT * FROM logs WHERE job_id = '替换为真实 job_id' ORDER BY id;
SELECT * FROM logs WHERE request_id = '替换为 X-Request-ID' ORDER BY id;
SELECT * FROM logs WHERE level IN ('ERROR', 'CRITICAL') ORDER BY id DESC;
```

日志是诊断记录，不替代业务检查点。强制杀进程可能丢失尚未写入的队列日志；
磁盘或数据库写入失败会输出到 stderr，不会使聊天事务失败。
