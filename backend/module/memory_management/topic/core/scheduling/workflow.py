"""Topic任务编排，复用公共重试规则。"""
from __future__ import annotations
from module.memory_management.shared.jobs.core.scheduling.workflow import MemoryWorkflow


class TopicWorkflow(MemoryWorkflow):
    """仅由TopicWorker驱动。"""
