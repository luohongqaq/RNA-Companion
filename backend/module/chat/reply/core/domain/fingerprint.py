"""提示词消息与完整候选的规范化hash，未来T2-B可复用同一规则。"""
from module.shared.common.fingerprints import CanonicalJsonFingerprint
from dataclasses import asdict
from collections.abc import Sequence
from module.character.core.domain.models import CharacterReply
from module.chat.reply.core.domain.materials import FrozenPrompt, PromptMessage


class ReplyFingerprint:
    @staticmethod
    def prompt_hash(messages: Sequence[PromptMessage]) -> str:
        payload = {"format": "reply-messages-v1", "messages": [
            {"role": message.role, "content": message.content} for message in messages]}
        return CanonicalJsonFingerprint.sha256(payload)

    @staticmethod
    def candidate_hash(reply: CharacterReply) -> str:
        return CanonicalJsonFingerprint.sha256(asdict(reply))

    @staticmethod
    def polish_context_hash(prompt: FrozenPrompt) -> str:
        return CanonicalJsonFingerprint.sha256({"tone_prompt": prompt.tone_prompt, "tone_prompt_version": prompt.tone_prompt_version,
                        "character_id": prompt.character_id,
                        "current_visual": asdict(prompt.current_visual) if prompt.current_visual else None,
                        "pose_consecutive_turns": prompt.pose_consecutive_turns})

    @staticmethod
    def generation_context_hash(prompt: FrozenPrompt) -> str:
        """保护冻结场景证据及锚点位置；旧润色hash保留给历史记录恢复。"""
        payload = {"scene_examples": prompt.scene_examples, "anchor_position": prompt.anchor_position}
        if prompt.visual_options is not None:
            payload["visual_options"] = asdict(prompt.visual_options)
        return CanonicalJsonFingerprint.sha256(payload)
