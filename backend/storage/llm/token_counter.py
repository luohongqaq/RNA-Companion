"""使用本地tokenizer及其聊天模板计数；不下载模型，不用字符数伪造token。"""
from collections.abc import Sequence
import hashlib
from pathlib import Path
from typing import Any
from module.chat.reply.core.domain.materials import PromptMessage


class TokenizerTokenCounter:
    def __init__(self, tokenizer_version: str = 'unconfigured', *, tokenizer: Any = None) -> None:
        self._tokenizer_version = tokenizer_version
        self._tokenizer = tokenizer

    def load_directory(self, path: Path) -> None:
        from transformers import AutoTokenizer, PreTrainedTokenizerFast
        path = Path(path).resolve()
        if not path.is_dir():
            raise ValueError('回复tokenizer目录不存在')
        # 完整 tokenizer.json 已包含分词规则，直接使用其 fast 后端。
        # 避免 transformers 根据旧 tokenizer_class 重建另一套分词器而漏计中文。
        loader = PreTrainedTokenizerFast if (path / "tokenizer.json").is_file() else AutoTokenizer
        tokenizer = loader.from_pretrained(str(path), local_files_only=True, trust_remote_code=False)
        if not tokenizer.chat_template:
            raise ValueError('回复tokenizer缺少chat_template，无法计入消息协议开销')
        digest = hashlib.sha256()
        for item in sorted(path.iterdir()):
            if item.is_file() and (item.name.startswith(('tokenizer', 'vocab', 'merges', 'chat_template'))
                                   or item.name in ('special_tokens_map.json', 'added_tokens.json')):
                digest.update(item.name.encode('utf-8'))
                digest.update(item.read_bytes())
        self._tokenizer = tokenizer
        self._tokenizer_version = f'local-chat-template-v2:{digest.hexdigest()}'
        # 在启动期间验证模板，而不是等用户首次发送才失败。
        self.count_messages((PromptMessage('user', 'test'),))

    @property
    def tokenizer_version(self) -> str:
        return self._tokenizer_version

    def count_text(self, text: str) -> int:
        """Count actual text tokens without chat-template framing."""
        if self._tokenizer is None:
            raise ValueError('回复tokenizer尚未加载')
        return len(self._tokenizer.encode(text, add_special_tokens=False))

    def count_messages(self, messages: Sequence[PromptMessage]) -> int:
        if self._tokenizer is None:
            raise ValueError('回复tokenizer尚未加载')
        if not messages:
            return 0
        encoded = self._tokenizer.apply_chat_template(
            [{'role': message.role, 'content': message.content} for message in messages],
            tokenize=True, return_dict=False, add_generation_prompt=messages[-1].role != 'assistant',
        )
        if not isinstance(encoded, list) or any(type(token) is not int for token in encoded):
            raise ValueError('tokenizer未返回单个消息序列的token ID列表')
        return len(encoded)
