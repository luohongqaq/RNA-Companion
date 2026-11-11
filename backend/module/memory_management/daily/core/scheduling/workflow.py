"""Daily任务编排，复用公共阶段重试规则。"""
from __future__ import annotations
from module.memory_management.shared.jobs.core.scheduling.workflow import MemoryWorkflow


class DailyWorkflow(MemoryWorkflow):
    """仅由DailyWorker驱动。"""
