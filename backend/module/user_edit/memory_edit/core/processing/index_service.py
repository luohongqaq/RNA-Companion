"""用户编辑后的固定版本索引重建服务。

将已领取编辑快照转换为 Daily 或 Topic 分区表示，在事务外写入向量库，再登记索引完成。
正式正文已在用户保存时切版，本服务不再次修改正文；过期版本和迟到代次由任务仓储核对。
"""
from __future__ import annotations
from module.shared.common.errors.base import InvalidInputError
from module.memory_management.shared.core.domain.document import VectorDocument
from module.shared.memory.types.references import MemoryKind, MemoryRevisionRef
from logger import stage
from module.memory_management.daily.core.domain.publication import DailyWrite
from module.memory_management.shared.contracts.errors import MemoryIndexUnavailable
from module.memory_management.topic.core.domain.publication import TopicWrite
from module.user_edit.memory_edit.core.domain.job import MemoryEdit
from module.user_edit.memory_edit.core.domain.ports import MemoryEditIndexPort, MemoryEditJobRepository


class MemoryEditIndexService:
    def __init__(self, repository: MemoryEditJobRepository, *,
                 index_repository: MemoryEditIndexPort | None = None) -> None:
        self._repository = repository
        self._index_repository = index_repository

    def configure_index(self, repository: MemoryEditIndexPort) -> None:
        """接入外部索引实现，正式正文保存流程不依赖该资源就绪。"""
        self._index_repository = repository

    @stage("memory.edit.index", background=True)
    async def index_memory_edit(self, edit: MemoryEdit) -> None:
        """使用领取时的固定快照编码；事务外访问Qdrant，不再次更新正式正文。"""
        if self._index_repository is None:
            raise MemoryIndexUnavailable("编辑索引服务尚未就绪")
        ref = MemoryRevisionRef(edit.kind, edit.document_id, edit.revision, edit.content_hash)
        write = (DailyWrite.from_document(edit.document) if edit.kind == MemoryKind.DAILY
                 else TopicWrite.from_document(edit.document))
        if write.content_hash != edit.content_hash:
            raise InvalidInputError("编辑快照与hash不一致")
        title = edit.document.title if edit.kind == MemoryKind.DAILY else edit.document.topic
        document = VectorDocument(f"{edit.document_id}:r{edit.revision}:{edit.content_hash}",
                                  ref, edit.character_id, edit.document, (), (), title)
        representations = write.index_representations(ref, edit.character_id, edit.recall_tier)
        if not await self._index_repository.batch_hybrid_index(document, representations):
            raise MemoryIndexUnavailable("编辑索引尚未确认完成")
        await self._repository.complete_memory_edit_index(edit.edit_id, edit.attempt_version)
