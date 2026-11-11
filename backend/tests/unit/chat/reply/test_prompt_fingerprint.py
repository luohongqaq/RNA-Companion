"""T2-B 使用的提示词指纹：保护内容、角色和顺序，不执行 I/O。"""

import pytest

from module.chat.reply.core.domain.fingerprint import ReplyFingerprint
from module.chat.reply.core.domain.materials import PromptMessage


def test_same_messages_have_same_fingerprint():
    messages = (PromptMessage("system", "你是桌宠"), PromptMessage("user", "你好\n🙂"))

    assert ReplyFingerprint.prompt_hash(messages) == ReplyFingerprint.prompt_hash(list(messages))


@pytest.mark.parametrize(
    "changed",
    [
        (PromptMessage("system", "你是桌宠"), PromptMessage("user", "不同正文")),
        (PromptMessage("system", "你是桌宠"), PromptMessage("assistant", "你好")),
        (PromptMessage("user", "你好"), PromptMessage("system", "你是桌宠")),
        (PromptMessage("system", "你是桌宠"), PromptMessage("user", "你好 ")),
    ],
    ids=["content", "role", "order", "whitespace"],
)
def test_changed_model_input_changes_fingerprint(changed):
    original = (PromptMessage("system", "你是桌宠"), PromptMessage("user", "你好"))

    assert ReplyFingerprint.prompt_hash(changed) != ReplyFingerprint.prompt_hash(original)


def test_message_boundaries_are_part_of_fingerprint():
    # 同样拼接出的正文，也不能把两条用户消息误当成一条。
    split = (PromptMessage("user", "ab"), PromptMessage("user", "c"))
    joined = (PromptMessage("user", "abc"),)

    assert ReplyFingerprint.prompt_hash(split) != ReplyFingerprint.prompt_hash(joined)
