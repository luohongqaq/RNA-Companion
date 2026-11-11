"""Preserve tokenizer.json rules even when config declares a different tokenizer class."""
import json

from tokenizers import Tokenizer, models, pre_tokenizers

from module.chat.reply.core.domain.materials import PromptMessage
from storage.llm.token_counter import TokenizerTokenCounter


def test_local_json_backend_preserves_chinese_tokens(tmp_path):
    text = "\u4f60\u597d\u4e16\u754c"
    tokenizer = Tokenizer(models.WordLevel({"[UNK]": 0, text: 1, "reply": 2}, unk_token="[UNK]"))
    tokenizer.pre_tokenizer = pre_tokenizers.Whitespace()
    tokenizer.save(str(tmp_path / "tokenizer.json"))
    (tmp_path / "tokenizer_config.json").write_text(json.dumps({
        "tokenizer_class": "LlamaTokenizerFast",
        "unk_token": "[UNK]",
        "chat_template": "{% for message in messages %}{{ message['content'] }} {% endfor %}{% if add_generation_prompt %}reply{% endif %}",
    }), encoding="utf-8")

    counter = TokenizerTokenCounter()
    counter.load_directory(tmp_path)
    assert counter.count_messages((PromptMessage("user", text),)) == 2
    assert counter.count_messages((PromptMessage("user", text), PromptMessage("assistant", text))) == 2
    assert counter.count_messages(()) == 0
    assert counter.tokenizer_version.startswith("local-chat-template-v2:")


def test_settings_defaults_to_bundled_deepseek_tokenizer(monkeypatch):
    from pathlib import Path
    from storage.config.settings import Settings
    monkeypatch.delenv("RNA_REPLY_TOKENIZER_PATH", raising=False)
    settings = Settings(_env_file=None)
    expected = Path(__file__).resolve().parents[4] / "resources" / "deepseek_v4_tokenizer"
    assert settings.reply_tokenizer_path == expected
