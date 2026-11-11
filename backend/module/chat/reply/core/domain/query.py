"""生成任务面向调用方的只读查询结果，不暴露执行材料。"""

from dataclasses import dataclass
from datetime import datetime

from module.chat.conversation.core.domain.contracts import AcceptedReply
from module.chat.reply.core.domain.models import GenerationFailure, GenerationJobStatus


@dataclass(frozen=True, slots=True)
class GenerationJobResult:
    job_id: str
    turn_id: str
    input_version: int
    status: GenerationJobStatus
    retry_count: int
    available_at: datetime
    error: GenerationFailure | None
    reply: AcceptedReply | None
    manual_retry_count: int = 0
