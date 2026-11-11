"""用户修改记忆、查询编辑状态和手动重试的业务入口。

先校验公开正文并按旧版本保留未改动内容的证据，再提交原子保存命令；成功后只通知后台索引。
保存流程不调用生成或审核模型，也不等待编码器就绪或索引完成。
"""
from __future__ import annotations
import logging
from collections.abc import Callable
from module.shared.common.errors.base import InvalidInputError, NotFoundError
from module.shared.memory.types.references import MemoryKind
from logger import stage
from module.user_edit.memory_edit.core.domain.command import SaveMemoryEdit, UpdateDailyMemory, UpdateTopicMemory
from module.user_edit.memory_edit.core.domain.ports import MemoryEditRepository
from module.user_edit.memory_edit.core.domain.result import MemoryEditDetails, MemoryEditResult
from module.user_edit.memory_edit.core.domain.document import DailyDocumentContent, TopicDocumentContent


class MemoryEditService:
    def __init__(self, repository: MemoryEditRepository, *,
                 notify_index: Callable[[], None] | None = None) -> None:
        self._repository = repository
        self._notify_index = notify_index

    def set_index_notifier(self, notify: Callable[[], None]) -> None:
        """保存成功后只发送唤醒信号，不等待向量编码。"""
        self._notify_index = notify

    @staticmethod
    def _validate_edit(command) -> None:
        for value in (command.character_id, command.document_id, command.idempotency_key):
            if not isinstance(value, str) or not 1 <= len(value.strip()) <= 128:
                raise InvalidInputError("角色、文档及幂等键必须为1～128个字符")
        if type(command.expected_revision) is not int or command.expected_revision < 1:
            raise InvalidInputError("expected_revision必须为正整数")

    @stage("memory.edit.save_daily")
    async def update_daily_memory(self, command: UpdateDailyMemory) -> MemoryEditResult:
        """用户正文直接保存，不调用生成或审核模型；日记日期和来源session保持不变。"""
        self._validate_edit(command)
        content = DailyDocumentContent.validate(command.document)
        memory = await self._repository.get_memory_for_edit(character_id=command.character_id,
            kind=MemoryKind.DAILY, document_id=command.document_id)
        if memory is None:
            raise NotFoundError("日记不存在")
        document = content.apply_to(memory)
        return await self._save_edit(command, MemoryKind.DAILY, document)

    @stage("memory.edit.save_topic")
    async def update_topic_memory(self, command: UpdateTopicMemory) -> MemoryEditResult:
        """允许清空时间线；保留的状态、更正关系必须引用当前仍存在的条目。"""
        self._validate_edit(command)
        content, document = TopicDocumentContent.validate(command.document)
        memory = await self._repository.get_memory_for_edit(character_id=command.character_id,
            kind=MemoryKind.TOPIC, document_id=command.document_id)
        if memory is None:
            raise NotFoundError("主题不存在")
        document = content.apply_to(document, memory)
        return await self._save_edit(command, MemoryKind.TOPIC, document)

    async def _save_edit(self, command, kind, document) -> MemoryEditResult:
        result = await self._repository.save_memory_edit(SaveMemoryEdit(command.character_id, kind,
            command.document_id, command.expected_revision, command.idempotency_key, document))
        self._notify_memory_index()
        return result

    def _notify_memory_index(self) -> None:
        if self._notify_index is not None:
            try:
                self._notify_index()
            except Exception:
                logging.getLogger(__name__).warning("编辑已保存，索引通知未送达；后台轮询将继续处理")

    @staticmethod
    def _validate_edit_id(edit_id: str) -> None:
        if not isinstance(edit_id, str) or not 1 <= len(edit_id.strip()) <= 128:
            raise InvalidInputError("编辑ID必须为1～128个字符")

    async def get_memory_edit(self, edit_id: str) -> MemoryEditDetails:
        self._validate_edit_id(edit_id)
        result = await self._repository.get_memory_edit(edit_id)
        if result is None:
            raise NotFoundError("编辑记录不存在")
        return result

    @stage("memory.edit.manual_retry")
    async def retry_memory_edit(self, edit_id: str) -> MemoryEditDetails:
        """只重新排队索引，返回登记后的状态；不在请求中等待向量编码。"""
        self._validate_edit_id(edit_id)
        result = await self._repository.retry_failed_memory_edit_index(edit_id)
        self._notify_memory_index()
        return result
