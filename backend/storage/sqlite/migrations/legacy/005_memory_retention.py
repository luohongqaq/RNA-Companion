"""补齐正式 Daily 和 Topic 的留存起算日期。"""

from datetime import datetime
from zoneinfo import ZoneInfo


async def migrate(connection):
    today = datetime.now(ZoneInfo("Asia/Shanghai")).date().isoformat()
    for table in ("daily_memories", "topic_memories"):
        await connection.exec_driver_sql(f"ALTER TABLE {table} ADD COLUMN last_mentioned_at DATE")
        await connection.exec_driver_sql(
            f"UPDATE {table} SET last_mentioned_at = ? "
            "WHERE published_at IS NOT NULL AND document_json IS NOT NULL AND vector_document_key IS NOT NULL",
            (today,),
        )
