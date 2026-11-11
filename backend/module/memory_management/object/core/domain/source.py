"""Object 提取使用的封存会话来源材料。

ObjectSourceInput 组合整个会话的完整轮和固定继承上文，按消息顺序构造模型输入与来源清单。
原始消息属于本次提取范围，继承上文只辅助消歧；来源哈希用于记录本次使用的固定材料。
"""
from __future__ import annotations
import hashlib
import json
from module.shared.common.time import BackendTime
from module.shared.context.types.messages import CompletedTurnResult
from module.shared.context.types.working import InheritedContext
from dataclasses import asdict, dataclass
from module.memory_management.shared.contracts.source import MemorySourceManifest


@dataclass(frozen=True, slots=True)
class ObjectSourceInput:
    """Object提取输入；拥有整个封存session，固定继承上文仅用于理解指代。"""
    session_id: str
    character_id: str
    owned_turns: tuple[CompletedTurnResult, ...]
    inherited_context: InheritedContext | None = None

    def source_manifest(self) -> MemorySourceManifest:
        """固定本次拥有的消息；继承材料参与hash，但不扩大新增事实范围。"""
        payload = self.to_model_payload()
        encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)
        return MemorySourceManifest(
            character_id=self.character_id, source_session_ids=(self.session_id,),
            source_message_ids=tuple(message["message_id"] for message in payload["messages"]),
            source_content_hash=hashlib.sha256(encoded.encode("utf-8")).hexdigest(),
        )

    def to_model_payload(self) -> dict:
        """按完整轮顺序展开 UTC 消息时间和后端当地日期。"""
        messages = []
        for turn in self.owned_turns:
            turn_messages = [("user", message) for message in turn.user_messages]
            turn_messages.append(("assistant", turn.accepted_reply))
            for role, message in turn_messages:
                messages.append({
                    "message_id": message.message_id,
                    "session_id": self.session_id,
                    "role": role,
                    "content": message.content,
                    "created_at": BackendTime.utc(message.created_at).isoformat(),
                    "local_date": BackendTime.local(message.created_at).date().isoformat(),
                })
        return {
            "session_id": self.session_id, "messages": messages,
            "INHERITED_CONTEXT": asdict(self.inherited_context) if self.inherited_context is not None else None,
        }
