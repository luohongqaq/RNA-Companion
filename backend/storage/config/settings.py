"""Typed process settings without importing configuration into core code."""

from pathlib import Path
from typing import Literal

from pydantic_settings import BaseSettings, SettingsConfigDict
from pydantic import Field, SecretStr, model_validator


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="RNA_",
        env_file=".env",
        extra="ignore",
        populate_by_name=True,
    )

    data_root: Path = Path("memory_record/database")
    log_database_path: Path = Path("memory_record/database/logs.sqlite3")
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"] = "INFO"
    log_retention_days: int = Field(default=30, ge=1)
    host: str = "127.0.0.1"
    port: int = 8765
    memory_production_enabled: bool = False
    memory_production_poll_seconds: float = Field(default=1.0, gt=0, allow_inf_nan=False)
    memory_enabled: bool = False
    memory_api_key: SecretStr | None = None
    memory_base_url: str | None = None
    memory_selection_model: str = ""
    memory_summary_model: str = ""
    memory_summary_api_key: SecretStr | None = None
    memory_summary_base_url: str | None = None
    memory_qdrant_path: Path | None = None
    memory_embedding_path: Path | None = Path("models/bge-m3")
    memory_reranker_path: Path = Path("models/qwen3-reranker-0.6b")
    memory_reranker_device: str = "cuda:0"
    memory_reranker_batch_size: int = Field(default=4, gt=0)
    memory_reranker_max_length: int = Field(default=2048, gt=0)
    memory_object_min_rerank_score: float = Field(default=0.15, ge=0, le=1, allow_inf_nan=False)
    memory_topic_min_rerank_score: float = Field(default=0.15, ge=0, le=1, allow_inf_nan=False)
    memory_legacy_character_id: str | None = None
    memory_send_thinking_parameter: bool = True
    memory_total_seconds: float = 12.0
    memory_target_seconds: float = 11.0
    memory_selection_seconds: float = 10.0
    memory_prefetch_enabled: bool = True
    reply_enabled: bool = False
    reply_api_key: SecretStr | None = None
    reply_base_url: str = "https://open.bigmodel.cn/api/paas/v4"
    reply_model: str = "glm-5.3-flashx"
    reply_temperature: float = 1.0
    reply_max_output_tokens: int = 500
    reply_timeout_seconds: float = 30.0
    reply_reasoning_effort: str | None = "low"
    generation_enabled: bool = False
    generation_poll_seconds: float = Field(default=1.0, gt=0, allow_inf_nan=False)
    context_enabled: bool | None = None #未指定时跟随自动回复开关，可单独启停上下文维护
    context_poll_seconds: float = Field(default=1.0, gt=0, allow_inf_nan=False)
    # 统一使用项目内的 DeepSeek tokenizer；独立于回复模型的供应商。
    reply_tokenizer_path: Path | None = (
        Path(__file__).resolve().parents[2] / "resources" / "deepseek_v4_tokenizer"
    )
    reply_scene_enabled: bool = True
    reply_anchor_position: Literal["system_end", "before_user"] = "before_user"
    reply_example_format: Literal["embedded", "dialogue"] = "dialogue"
    # 缺省时由 seed_scenes 按已登记角色的 scenes/sessions_train_by_expr 推导
    reply_scene_source_path: Path | None = None
    reply_scene_qdrant_path: Path = Path("memory_record/database/scene_qdrant")
    reply_scene_threshold: float = 0.75
    reply_scene_top_k: int = 1
    reply_scene_candidates: int = 10
    default_api_key: SecretStr | None = Field(default=None, validation_alias="DEFAULT_API_KEY")
    default_base_url: str = Field(default="https://api.deepseek.com", validation_alias="DEFAULT_BASE_URL")
    default_model: str = Field(default="deepseek-chat", validation_alias="DEFAULT_MODEL")

    @model_validator(mode="after")
    def memory_defaults(self):
        # 本项目既有DEFAULT_*用于DeepSeek；绝不覆盖独立的GLM回复配置。
        self.memory_api_key = self.memory_api_key or self.default_api_key
        self.memory_base_url = self.memory_base_url or self.default_base_url
        self.memory_selection_model = self.memory_selection_model or self.default_model
        self.memory_summary_model = self.memory_summary_model or self.memory_selection_model
        # 总结的地址和密钥分别回退到筛选配置。
        self.memory_summary_api_key = self.memory_summary_api_key or self.memory_api_key
        self.memory_summary_base_url = self.memory_summary_base_url or self.memory_base_url
        return self

