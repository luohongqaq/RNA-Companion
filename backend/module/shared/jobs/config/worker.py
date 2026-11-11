"""module.shared.jobs.config.worker: 业务类型与配置，不执行外部I/O。"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class WorkerPoolConfig:
    """进程配置，不逐任务存储；前台额度独立，Context/Storage固定1槽。

    配置加载时应检查context_slots=storage_slots=1、
    1<=memory_slots<=memory_max_slots以及foreground_slots>=1。
    """
    foreground_slots: int = 1
    context_slots: int = 1
    memory_slots: int = 2
    memory_max_slots: int = 2
    storage_slots: int = 1
