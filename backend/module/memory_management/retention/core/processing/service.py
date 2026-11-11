"""启动时处理到期热记忆与待检查object，先同步索引再提交正式池状态。"""

from datetime import date
import logging

from module.memory_management.retention.core.domain.ports import MemoryPoolIndex, MemoryRetentionRepository
from module.shared.memory.types.retention import RecallTier

logger = logging.getLogger(__name__)


class MemoryRetentionService:
    """把到期记忆移出热池；daily、topic、object共用同一衰减规则。"""

    HALF_LIFE_DAYS = 7
    HOT_THRESHOLD = 0.1

    def __init__(self, repository: MemoryRetentionRepository, index: MemoryPoolIndex) -> None:
        self._repository = repository
        self._index = index

    @staticmethod
    def _evaluate_retention(last_mentioned_at: date, today: date) -> tuple[float, RecallTier]:
        elapsed_days = max(0, (today - last_mentioned_at).days)
        score = 0.5 ** (elapsed_days / MemoryRetentionService.HALF_LIFE_DAYS)
        tier = RecallTier.HOT if score >= MemoryRetentionService.HOT_THRESHOLD else RecallTier.COLD
        return score, tier

    async def cool_expired_memories(self, today: date) -> int:
        synced = []
        for memory in await self._repository.list_hot_memories():
            _, target = self._evaluate_retention(memory.last_mentioned_at, today)
            if target == RecallTier.HOT:
                continue
            if await self._index.set_memory_pool(memory.character_id, memory.ref, target):
                synced.append(memory)
            else:
                logger.warning("当前正式版本缺少索引分区，暂缓降池：%s", memory.ref.memory_id)

        if synced:
            await self._repository.cool_down_batch(tuple(synced))
            logger.info("启动时转入记忆冷池 %d 条", len(synced))
        return len(synced)

    async def cool_unlinked_objects(self, today: date) -> int:
        """未并入topic且允许淡出的object，按得知日衰减后退出热池。"""
        synced = []
        for item in await self._repository.list_hot_unlinked_objects():
            _, target = self._evaluate_retention(item.mentioned_on, today)
            if target == RecallTier.HOT:
                continue
            if await self._index.set_object_pool(item.character_id, item.object_id, target):
                synced.append(item)
            else:
                logger.warning("当前object缺少索引分区，暂缓降池：%s", item.object_id)

        if synced:
            await self._repository.cool_down_objects(tuple(synced))
            logger.info("启动时转入object冷池 %d 条", len(synced))
        return len(synced)
