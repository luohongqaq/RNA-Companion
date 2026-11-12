"""T2-B之后、T3之前的单阶段回复：使用已冻结的记忆、场景与锚点，一次生成台词和动作。"""
from logger import stage
import asyncio
import json
import hashlib
from dataclasses import asdict
from collections.abc import Callable
from datetime import datetime, timezone
from module.chat.reply.core.domain.ports import ReplyModelPort
from module.character.core.domain.models import CharacterReply
from module.shared.context.types.visual import VisualState
from module.shared.character.types.manifest import VisualOptions
from module.chat.reply.core.domain.config.reply import ReplyModelConfig
from module.chat.reply.core.domain.errors.reply import ReplyOutputError, ReplyPromptMismatch, ReplyTruncatedError
from module.chat.reply.core.domain.fingerprint import ReplyFingerprint
from module.chat.reply.core.domain.materials import (
    FrozenPrompt, ModelReply, ReplyCandidate, ReplyGenerationResult, ReplyModelResponse,
)


# 模型偶发在句中停笔：finish_reason为stop、JSON也完整，但台词没说完。
# 这些词只能接续下文，落在一句结尾就是没写完。
_UNFINISHED_TAIL = ("是", "把", "到", "的", "和", "跟", "在", "就", "还", "又", "而", "但", "或",
                    "被", "让", "给", "比", "像", "为", "从", "向", "对", "与", "且", "并")


class GenerationReplyService:
    @staticmethod
    def validate_reply_visual(reply: CharacterReply, options: VisualOptions | None = None) -> None:
        if reply.visual is None:
            raise ReplyOutputError("回复缺少视觉状态")
        # 老冻结记录和没有资源清单的角色仍遵守 JSON 结构校验。
        if options is None:
            return
        names = reply.visual.names()
        if names["expression_name"] not in options.expressions:
            raise ReplyOutputError("回复返回了未允许的表情名称")
        if names["pose_name"] not in options.poses:
            raise ReplyOutputError("回复返回了未允许的姿势名称")

    def __init__(self, model: ReplyModelPort, config: ReplyModelConfig | None = None, *,
                 visual_resolver: Callable[[str, str], VisualState] = VisualState.from_names) -> None:
        self._model = model
        self._config = config or ReplyModelConfig()
        self._visual_resolver = visual_resolver

    async def _request_model(self, prompt: FrozenPrompt) -> ReplyModelResponse:
        """在reply.py直接请求模型"""
        return await self._request_messages(prompt.messages)

    async def _request_messages(self, messages: tuple) -> ReplyModelResponse:
        return await self._model.complete(messages, self._config)

    @stage("generation.model")
    async def generate(
        self, prompt: FrozenPrompt, *, expected_prompt_hash: str,
        is_current: Callable[[], bool] | None = None,
    ) -> ReplyGenerationResult:
        """正式接入时由调用方保证T2-B已提交；本函数不伪造或修改事务状态。
        候选尚未写库；普通调试可显式提供固定消息及其hash，不能自动回填聊天记录。
        """
        def ensure_current() -> None:
            if is_current is not None and not is_current():
                raise asyncio.CancelledError("输入已过时，丢弃回复结果")
        ensure_current()
        if not prompt.messages or expected_prompt_hash != ReplyFingerprint.prompt_hash(prompt.messages):
            raise ReplyPromptMismatch("消息与本次固定的prompt_hash不一致")
        response = await self._request_model(prompt)
        ensure_current()
        self._require_finished(response)
        reply = self.parse_character_reply(self._unwrap_json(response.content), self._visual_resolver)
        self._require_sentence_finished(reply)
        self.validate_reply_visual(reply, prompt.visual_options)
        trace = {
            "schema": "single-stage-scene-reply-v1",
            "reply": reply.response_zh, "reply_visual": asdict(reply.visual),
            "request_id": response.model_request_id,
            "usage": asdict(response.usage) if response.usage else None,
            "scenes": list(prompt.scene_examples), "no_scene_reference": not bool(prompt.scene_examples),
            "anchor_position": prompt.anchor_position,
            "original_prompt_hash": expected_prompt_hash, "final_reply_hash": ReplyFingerprint.candidate_hash(reply),
        }
        trace["trace_hash"] = hashlib.sha256(json.dumps(trace, ensure_ascii=False, sort_keys=True).encode()).hexdigest()
        result = ModelReply(reply, response.model_request_id, response.usage)
        candidate = ReplyCandidate(reply, ReplyFingerprint.candidate_hash(reply), expected_prompt_hash, response.usage,
                                   datetime.now(timezone.utc), generation_trace=trace)
        return ReplyGenerationResult(result, candidate)

    @staticmethod
    def _require_finished(response: ReplyModelResponse) -> None:
        if response.finish_reason == "length":
            raise ReplyTruncatedError("回复达到输出上限，不能作为完整候选")
        if response.finish_reason != "stop":
            raise ReplyOutputError("模型未完整结束回复")

    @staticmethod
    def _require_sentence_finished(reply: CharacterReply) -> None:
        """拦住停在半句的台词；完整JSON不代表说完了一句话。"""
        text = reply.response_zh.strip()
        if text.endswith(_UNFINISHED_TAIL):
            raise ReplyTruncatedError("回复停在半句，不能作为完整候选")

    @staticmethod
    def _unwrap_json(content: str) -> str:
        clean = content.strip()
        if clean.startswith("```json\n") and clean.endswith("```"):
            clean = clean[len("```json\n"):-3].strip()
        elif clean.startswith("```\n") and clean.endswith("```"):
            clean = clean[4:-3].strip()
        return clean

    def parse_character_reply(
            self, content: str, resolver: Callable[[str, str], VisualState] = VisualState.from_names,
    ) -> CharacterReply:
        """严格读取response_zh及visual名称；不从思考文本、代码块或半段JSON补造答案。"""
        try:
            data = json.loads(content, object_pairs_hook = self._unique_object)
            if not isinstance(data, dict) or set(data) != {"response_zh", "visual"}:
                raise ValueError("回复字段必须为response_zh和visual")
            text, visual = data["response_zh"], data["visual"]
            if not isinstance(text, str) or not text.strip():
                raise ValueError("回复正文为空")
            if not isinstance(visual, dict) or set(visual) != {"pose_name", "expression_name"}:
                raise ValueError("visual必须包含pose_name和expression_name")
            if not all(isinstance(value, str) and value.strip() for value in visual.values()):
                raise ValueError("视觉名称必须为非空字符串")
            state = resolver(visual["pose_name"].strip(), visual["expression_name"].strip())
            if not isinstance(state, VisualState):
                raise ValueError("视觉映射未返回VisualState")
            return CharacterReply(text.strip(), state)
        except (ValueError, TypeError, KeyError) as exc:
            raise ReplyOutputError("模型回复不符合response_zh/visual契约") from exc

    @staticmethod
    def _unique_object(pairs: list[tuple[str, object]]) -> dict:
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("回复JSON含重复字段")
            result[key] = value
        return result
