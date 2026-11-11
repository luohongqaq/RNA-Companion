"""跨模块传递的连续轮次范围与当前轮输入引用。"""
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class TurnRange:
    """连续完整轮范围"""
    session_id: str #所属会话id
    from_seq: int #起始轮次序号（含）
    through_seq: int #结束轮次序号（含）


@dataclass(frozen=True, slots=True)
class CurrentTurnRef:
    """当前轮引用"""
    turn_id: str #轮次id
    input_version: int #输入版本
    message_ids: tuple[str, ...] #有序消息id
