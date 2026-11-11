"""仅供重启验收调用的独立子进程；在已提交检查点模拟进程崩溃。"""
import asyncio
import json
import os
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from module.chat.conversation.core.domain.contracts import SaveMessageInput
from module.chat.reply.core.processing.context_loader import ContextLoader
from module.chat.reply.core.processing.completion import GenerationCompletionService
from module.chat.reply.core.processing.preparation import GenerationPreparationService
from module.chat.reply.core.processing.prompt_builder import PromptBuilder
from module.chat.reply.core.processing.service import GenerationReplyService
from module.shared.jobs.config.retry import RetryPolicy
from module.chat.reply.core.scheduling.worker import GenerationWorker
from module.chat.reply.core.scheduling.workflow import GenerationWorkflow
from module.memory_recall.agent.core.processing.config.pipeline import MemoryRecallConfig
from module.memory_recall.agent.core.processing.service import MemoryRecallService
from module.memory_recall.agent.repository.resources import MemoryRecallResourceLoader
from module.chat.reply.repository.prompt_resources import FileReplyPromptResourceReader
from module.chat.conversation.repository.sqlite import SQLiteChatRepository
from module.chat.reply.repository.context_sqlite import SQLiteContextReadRepository
from storage.sqlite.db_session import SQLiteDatabase
from module.chat.reply.repository.sqlite import SQLiteGenerationRepository
from test_full_chat import (
    QUESTION, FixedMemoryReaderFactory, FixedTokenCounter, ScriptedMemoryModel, ScriptedReplyClient,
)
from module.chat.reply.repository.llm import ReplyModelRepository


async def run(mode, checkpoint, database_path, audit_path):
    def audit(name):
        with Path(audit_path).open("a", encoding="utf-8") as file:
            file.write(json.dumps({"mode": mode, "name": name}) + "\n")
            file.flush()
            os.fsync(file.fileno())

    def crash_at(name):
        if mode == "interrupt" and checkpoint == name:
            audit("crash:" + name)
            os._exit(42)  # 刻意跳过 finally、Worker.stop 和数据库正常关闭。

    class Repository(SQLiteGenerationRepository):
        async def claim_job(self, job_id):
            ticket = await super().claim_job(job_id)
            if ticket is not None:
                crash_at("retrieving")
            return ticket

        async def freeze_prompt(self, ticket):
            result = await super().freeze_prompt(ticket)
            if result:
                crash_at("prompt")
            return result

        async def save_candidate(self, ticket, candidate):
            result = await super().save_candidate(ticket, candidate)
            if result:
                crash_at("candidate")
            return result

        async def accept_reply(self, ticket, **kwargs):
            result = await super().accept_reply(ticket, **kwargs)
            if result is not None:
                crash_at("committed")
            return result

    class MemoryModel(ScriptedMemoryModel):
        async def complete(self, request):
            audit("memory:" + request.model)
            return await super().complete(request)

    class ReplyClient(ScriptedReplyClient):
        async def create(self, **kwargs):
            audit("reply_request")
            result = await super().create(**kwargs)
            audit("reply_response")
            crash_at("model_returned")
            return result

    db = SQLiteDatabase(Path(database_path))
    worker = None
    try:
        await db.initialize()
        repository = Repository(db)
        # 固定 request_id：重启重放原回执，不重复登记用户消息。
        saved = await SQLiteChatRepository(db).accept_message(SaveMessageInput(
            "restart_request_0001", "restart_conv", "character_test", QUESTION,
        ))
        crash_at("queued")
        if mode == "interrupt" and checkpoint == "cancelled":
            await repository.cancel_job(saved.job_id)
            crash_at("cancelled")
        counter = FixedTokenCounter()
        memory = MemoryRecallService(
            MemoryModel(), FixedMemoryReaderFactory(), MemoryRecallResourceLoader.load(),
            MemoryRecallConfig(selection_model="test-selection", summary_model="test-summary",
                               prefetch_enabled=False),
            token_counter=counter,
        )
        preparation = GenerationPreparationService(
            ContextLoader(SQLiteContextReadRepository(db)), memory,
            prompt_resources=FileReplyPromptResourceReader(db), prompt_builder=PromptBuilder(counter),
        )
        completion = GenerationCompletionService(repository, GenerationReplyService(ReplyModelRepository(ReplyClient())), counter)
        worker = GenerationWorker(repository, GenerationWorkflow(
            repository, preparation, completion, retry_policy=RetryPolicy(max_retries=0),
        ), poll_interval=0.01)
        await worker.start()
        async with asyncio.timeout(15):
            while True:
                job = await repository.get_job(saved.job_id)
                if job.status.value == "done":
                    break
                if checkpoint == "cancelled" and job.status.value == "cancelled":
                    break
                if job.status.value == "failed":
                    raise AssertionError(f"恢复失败：{job.last_error}")
                await asyncio.sleep(0.01)
        # done 必须已从可调度列表移除，重启不会再执行它。
        assert await repository.list_ready_jobs() == ()
        audit("finished")
    finally:
        if worker is not None:
            await worker.stop()
        await db.close()


if __name__ == "__main__":
    asyncio.run(run(*sys.argv[1:]))
