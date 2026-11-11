"""回复备料所用的场景召回接口。"""
from typing import Protocol

from module.chat.scenes_recall.core.domain.models import SceneExample, SceneQuery


class SceneRecallPort(Protocol):
    async def search(self, query: SceneQuery) -> tuple[SceneExample, ...]:
        """依据聊天情境召回表达示例，不能把草稿或长期记忆作为查询。"""
        ...
