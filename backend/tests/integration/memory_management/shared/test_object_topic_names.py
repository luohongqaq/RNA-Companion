"""当前 object/result/entry 协议直接持久化，不经过旧字段转换。"""
import hashlib
import json
import pytest
from module.memory_management.shared.jobs.repository import mappers as job_mappers
from test_memory_job_mapping import make_job

@pytest.mark.parametrize("variant", ["daily", "topic", "update"])
def test_current_job_json_round_trip(variant):
    job = make_job(variant)
    row = job_mappers.memory_job_to_row(job)
    assert job_mappers.memory_job_to_domain(row) == job
    assert job_mappers.memory_job_payload_to_json(job) == row.payload_json
    if variant != "daily":
        result = row.payload_json["generation_result"]
        assert "result_version" in result and "candidate_version" not in result
        document = result["applications"][0]["document"]
        assert "entries" in document and "events" not in document

def test_topic_hash_covers_saved_document():
    document = make_job("topic").generation_result.applications[0].document
    encoded = json.dumps(document.to_data(), ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    assert document.content_hash() == hashlib.sha256(encoded.encode()).hexdigest()
