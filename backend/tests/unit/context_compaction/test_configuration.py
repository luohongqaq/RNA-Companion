"""Configuration and window reservation contracts."""
from dataclasses import replace
import pytest
from module.context_compaction.core.domain.lifecycle_policy import SessionLifecycleConfig
from module.context_compaction.core.domain.policy import ContextCompactionPolicy
from module.chat.reply.core.domain.config.context_budget import ContextPromptBudget


@pytest.mark.parametrize("changes", [
    {"session_target_tokens": 0}, {"session_target_tokens": True},
    {"session_prepare_ratio": float("nan")}, {"session_hard_ratio": float("inf")},
    {"session_prepare_ratio": 1}, {"session_hard_ratio": 1.1},
    {"handoff_wait_seconds": 0},
])
def test_lifecycle_rejects_invalid_configuration(changes):
    with pytest.raises(ValueError):
        SessionLifecycleConfig(**changes)
    with pytest.raises(ValueError):
        replace(SessionLifecycleConfig(), **changes)


@pytest.mark.parametrize("changes", [
    {"summary_token_limit": 0}, {"summary_target_tokens": 1000},
    {"summary_target_tokens": True}, {"max_compression_retries": -1},
])
def test_compaction_rejects_invalid_configuration(changes):
    with pytest.raises(ValueError):
        ContextCompactionPolicy(**changes)


@pytest.mark.parametrize("changes", [
    {"history_budget": 0}, {"safety_margin": -1}, {"reserved_output_tokens": True},
    {"effective_context_limit": 8000},
])
def test_prompt_budget_rejects_invalid_configuration(changes):
    with pytest.raises(ValueError):
        ContextPromptBudget(**changes)
