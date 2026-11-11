"""Object 提取任务的持久化输入与阶段结果。

ObjectJobInput 固定封存会话身份，ObjectJob 保存提取出的对象及取舍说明，供索引、激活和恢复使用。
任务输入构造登记命令和幂等键，任务本身校验类型和提取结果。
"""
from __future__ import annotations
from dataclasses import dataclass
from module.memory_management.object.core.domain.document import MemoryObject
from module.memory_management.object.core.domain.generation import ObjectSelectionNote
from module.memory_management.shared.jobs.core.domain.job import CreateMemoryJob, MemoryJob, MemoryJobInput, MemoryJobType


@dataclass(frozen=True, slots=True, kw_only=True)
class ObjectJobInput(MemoryJobInput):
    """直接读取封存session原文，不依赖daily。"""
    source_message_ids: tuple[str, ...] = ()

    @classmethod
    def create_command(cls, character_id: str, session_id: str, *, priority: int = 10) -> CreateMemoryJob:
        """一个封存session登记一次object提取；重试不改变登记身份。"""
        return CreateMemoryJob(
            job_type=MemoryJobType.EXTRACT_OBJECTS,
            target_key=f'object:{character_id}:{session_id}',
            dedupe_key=f'extract_objects:{character_id}:{session_id}',
            priority=priority,
            input=cls(character_id=character_id, source_session_ids=(session_id,)),
        )


@dataclass(slots=True)
class ObjectJob(MemoryJob):
    """独立保存object提取结果，随后索引、激活并供topic整合。"""
    input: ObjectJobInput
    objects: tuple[MemoryObject, ...] = ()
    selection_notes: tuple[ObjectSelectionNote, ...] = ()  # 调试材料，不进入正式object正文

    def __post_init__(self) -> None:
        if not isinstance(self.input, ObjectJobInput) or self.job_type != MemoryJobType.EXTRACT_OBJECTS:
            raise ValueError("ObjectJob的任务类型或输入不匹配")
        if any(not isinstance(candidate, MemoryObject) for candidate in self.objects):
            raise ValueError("ObjectJob只能保存object候选")
        if any(not isinstance(note, ObjectSelectionNote) for note in self.selection_notes):
            raise ValueError("ObjectJob取舍说明类型不匹配")
