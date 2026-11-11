"""module.shared.jobs.config.retry: 业务类型与配置，不执行外部I/O。"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta


@dataclass(frozen=True, slots=True)
class RetryPolicy:
    """按任务类型在进程配置中选择；max_retries不写入每条任务。

    暂时失败保留原status，只增加retry_count、更新available_at和错误摘要。
    重试超过给定退避表长度时复用最后一项；具体策略由worker实施。
    """
    max_retries: int = 3
    delays: tuple[timedelta, ...] = (
        timedelta(seconds=5), timedelta(seconds=15),
        timedelta(seconds=60), timedelta(minutes=5),
    )

    def __post_init__(self) -> None:
        if type(self.max_retries) is not int or self.max_retries < 0 or not self.delays or any(
            not isinstance(delay, timedelta) or delay.total_seconds() < 0 for delay in self.delays
        ):
            raise ValueError("重试次数和退避时间必须非负，退避表不能为空")
