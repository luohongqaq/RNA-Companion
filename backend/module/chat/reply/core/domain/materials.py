"""module.chat.reply.core.domain.materials: 业务类型与配置，不执行外部I/O。"""
from __future__ import annotations

from module.character.core.domain.models import CharacterReply, CharacterCard
from module.shared.context.types.turns import CurrentTurnRef, TurnRange
from module.shared.context.types.references import InheritedContextRef, SnapshotRef
from module.shared.memory.types.references import MemoryRevisionRef
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any
from module.shared.context.types.messages import PromptMessage as PromptMessage
from module.shared.context.types.visual import VisualState
from module.shared.character.types.manifest import VisualOptions


@dataclass(frozen=True, slots=True)
class GenerationInputBoundary:
    """T2-A固定的输入边界，规定允许读取哪些输入，生成期间不可变更"""
    conversation_id: str #对话id
    character_id: str #角色id
    session_id: str #会话id
    working_snapshot: SnapshotRef | None #工作摘要，第一版允许为空
    complete_turns: TurnRange #连续完整轮范围
    current_turn: CurrentTurnRef #当前轮引用
    inherited_context: InheritedContextRef | None #继承上下文，第一版允许为空

    @classmethod
    def fix(cls, *, conversation_id: str, character_id: str, session_id: str,
            turn_id: str, turn_seq: int, input_version: int, message_ids: Sequence[str],
            working_snapshot: SnapshotRef | None = None,
            inherited_context: InheritedContextRef | None = None) -> "GenerationInputBoundary":
        """T2-A固定输入边界：完整轮范围紧接摘要覆盖点（covered+1），无摘要则从会话第1轮读起"""
        covered = working_snapshot.covered_through_turn_seq if working_snapshot else 0
        return cls(
            conversation_id=conversation_id,
            character_id=character_id,
            session_id=session_id,
            working_snapshot=working_snapshot,
            complete_turns=TurnRange(session_id=session_id,
                                     from_seq=covered + 1, through_seq=turn_seq - 1),
            current_turn=CurrentTurnRef(turn_id=turn_id, input_version=input_version,
                                        message_ids=tuple(message_ids)),
            inherited_context=inherited_context,
        )


@dataclass(frozen=True, slots=True)
class ChatContextManifest:
    """实际使用的上下文清单，记录最终实际使用了哪些输入"""
    working_snapshot: SnapshotRef | None #实际使用的摘要
    complete_turns: TurnRange #完整轮范围
    current_turn: CurrentTurnRef #当前轮
    inherited_context: InheritedContextRef | None #继承来源


@dataclass(frozen=True, slots=True)
class MemoryManifest:
    """本次采用的记忆版本清单，第一版允许为空"""
    daily: tuple[MemoryRevisionRef, ...] = () #实际使用的daily版本
    topic: tuple[MemoryRevisionRef, ...] = () #实际使用的topic版本
    other: tuple[MemoryRevisionRef, ...] = () #diary、impression、shared等版本


@dataclass(frozen=True)
class ReplyPromptResources:
    """一次组装使用的角色与固定提示词正文；读取后在本次流程中直接复用。"""
    character_card: CharacterCard #完整角色卡
    character_card_version: str #角色卡版本，首版可使用内容hash
    system_prompt: str #第一阶段角色回复的系统提示词，不含语气润色阶段
    output_requirements: str #与回复解析契约一致的输出要求
    template_version: str #固定提示词与渲染规则版本
    tone_prompt: str = "" #语气提示词，保留给历史记录校验
    tone_prompt_version: str = "" #语气提示词内容hash

    visual_options: VisualOptions | None = None
    scene_recall_enabled: bool = False


@dataclass(frozen=True, slots=True)
class FrozenPrompt:
    """完整提示词材料；T2-B校验并持久化成功后才可调用模型"""
    messages: tuple[PromptMessage, ...] #完整提示词
    token_count: int | None #未绑定tokenizer时为None，不伪造计数
    context_manifest: ChatContextManifest #上下文清单
    memory_manifest: MemoryManifest #记忆清单
    character_card_version: str #角色卡版本
    tokenizer_version: str #tokenizer版本
    template_version: str = "" #本次资源与渲染规则版本
    tone_prompt: str = "" #保留历史冻结记录中的语气提示词
    tone_prompt_version: str = ""
    memory_summary: str = "" #最终提示词使用的记忆总结；不包含原始卡片
    memory_summary_hash: str = ""
    budget_checked: bool = False #只有实际计数并通过预算检查才为True
    character_id: str = ""
    current_visual: VisualState | None = None
    pose_consecutive_turns: int = 0
    visual_options: VisualOptions | None = None
    scene_examples: tuple[dict[str, Any], ...] = ()  # 已在T2-B前召回并冻结的场景证据。
    anchor_position: str = "system_end"


@dataclass(frozen=True, slots=True)
class PromptParts:
    """提示词分区内容"""
    character_setting: str #角色设定
    output_requirements: str #输出要求
    context: str = "" #上下文
    memory: str = "" #记忆，第一版允许为空
    few_shot: str = "" #示范


@dataclass(frozen=True, slots=True)
class ModelUsage:
    """模型用量"""
    input_tokens: int #输入token数
    output_tokens: int #输出token数
    total_tokens: int #总token数


@dataclass(frozen=True, slots=True)
class ReplyModelResponse:
    """回复模型原始响应，供业务层解析。"""
    content: str
    finish_reason: str
    model_request_id: str
    usage: ModelUsage | None


@dataclass(frozen=True, slots=True)
class ModelReply:
    """模型返回"""
    reply: CharacterReply #解析后的角色回复
    model_request_id: str #模型请求id
    usage: ModelUsage | None #服务未报告用量时为None，不伪造零值


@dataclass(frozen=True, slots=True)
class ReplyCandidate:
    """完整候选材料，持久化后用于T3；candidate_hash覆盖正文及完整视觉状态"""
    reply: CharacterReply #角色回复
    candidate_hash: str #候选哈希
    prompt_hash: str #对应的提示词哈希
    usage: ModelUsage | None #服务未报告用量时为None
    created_at: datetime #生成时间
    generation_trace: dict[str, Any] | None = None #草稿、场景版本与第二阶段证据；不是正式消息


@dataclass(frozen=True, slots=True)
class ReplyGenerationResult:
    """T3前的模型回复及完整候选材料。"""
    model_reply: ModelReply
    candidate: ReplyCandidate #尚未持久化，不代表已正式采用
