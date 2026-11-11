"""记忆任务表映射与按任务类型分派的编解码。"""

from typing import Any
from module.memory_management.object.core.domain.job import ObjectJob
from module.memory_management.daily.core.domain.job import DailyJob
from module.memory_management.topic.core.domain.job import TopicJob
from module.memory_management.shared.jobs.core.domain.job import MemoryJob
from storage.sqlite.models import MemoryJobRow
from module.memory_management.daily.repository.mappers import daily_job_payload_to_json, daily_job_to_domain
from module.memory_management.object.repository.mappers import (
    object_job_payload_to_json,
    object_job_to_domain,
)
from module.memory_management.topic.repository.mappers import topic_job_payload_to_json, topic_job_to_domain


def memory_job_to_row(value: MemoryJob) -> MemoryJobRow:
    """仅构造新任务行；已有任务更新时修改已加载的行。"""
    return MemoryJobRow(
        job_id=value.job_id,
        job_type=value.job_type.value,
        target_key=value.target_key,
        dedupe_key=value.dedupe_key,
        priority=value.priority,
        status=value.status.value,
        retry_count=value.retry_count,
        available_at=value.available_at,
        created_at=value.created_at,
        updated_at=value.updated_at,
        payload_json=memory_job_payload_to_json(value),
    )


def memory_job_payload_to_json(value: MemoryJob) -> dict[str, Any]:
    if isinstance(value, ObjectJob):
        return object_job_payload_to_json(value)
    if isinstance(value, DailyJob):
        return daily_job_payload_to_json(value)
    if isinstance(value, TopicJob):
        return topic_job_payload_to_json(value)
    raise ValueError("use DailyJob, ObjectJob or TopicJob instead of MemoryJob")


def memory_job_to_domain(row: MemoryJobRow) -> MemoryJob:
    if not isinstance(row.payload_json, dict):
        raise ValueError("memory_record job payload must be a JSON object")
    family = row.payload_json["job_family"]
    if family == "object":
        return object_job_to_domain(row)
    if family == "daily":
        return daily_job_to_domain(row)
    if family == "topic":
        return topic_job_to_domain(row)
    raise ValueError("unknown memory_record job family")
