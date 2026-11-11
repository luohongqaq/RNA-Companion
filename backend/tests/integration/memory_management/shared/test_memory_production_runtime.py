"""生产运行入口：真实本地Qdrant、假模型客户端，验证共享资源与启停。"""
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock
import pytest
from api import container as container_module
from storage.config import memory_runtime
from storage.config.settings import Settings
from storage.llm import client_factory
from module.memory_management.shared.indexing.repository.qdrant import QdrantMemoryIndexRepository
from module.memory_management.daily.repository.qdrant import DailyIndexRepository
from module.user_edit.memory_edit.repository.qdrant import MemoryEditIndexRepository
from module.memory_management.daily.core.scheduling.worker import DailyWorker
from module.memory_management.object.core.scheduling.worker import ObjectWorker
from module.memory_management.topic.core.scheduling.worker import TopicWorker
from module.memory_management.object.core.processing.config import ObjectModelConfig
from module.memory_management.object.core.domain.job import ObjectJobInput


def settings(tmp_path, **changes):
    values = dict(_env_file=None, data_root=tmp_path/"var", 
        memory_production_enabled=True, memory_enabled=False, reply_enabled=False,
        generation_enabled=False, context_enabled=False, memory_qdrant_path=tmp_path/"qdrant",
        memory_embedding_path=tmp_path/"model", reply_api_key="glm-test", default_api_key="ds-test",
        reply_base_url="https://glm.invalid", default_base_url="https://ds.invalid")
    values.update(changes)
    return Settings(**values)


def fake_resources(monkeypatch, *, object_response=None):
    encoder=object()
    load=Mock(return_value=encoder)
    monkeypatch.setattr(QdrantMemoryIndexRepository,"load_encoder",load)
    monkeypatch.setattr(QdrantMemoryIndexRepository,"encode_dual_vectors",Mock(return_value=[]))
    reranker = SimpleNamespace(close=Mock())
    monkeypatch.setattr(memory_runtime.QwenMemoryRerankRepository, "load", AsyncMock(return_value=reranker))
    clients=[]
    def client(**kwargs):
        result=SimpleNamespace(close=AsyncMock(), options=kwargs,
            chat=SimpleNamespace(completions=SimpleNamespace(create=AsyncMock(return_value=object_response))))
        clients.append(result)
        return result
    monkeypatch.setattr(client_factory,"AsyncOpenAI",client)
    return encoder,load,clients


@pytest.mark.asyncio
async def test_production_only_initializes_once_and_stops_worker_before_resources(tmp_path,monkeypatch):
    encoder,load,clients=fake_resources(monkeypatch)
    container=container_module.build_container(settings(tmp_path))
    try:
        await container.start()
        assert container.generation_preparation_service._memory_recall is None
        assert container.memory_storage.get_encoder() is encoder
        assert container.daily_worker is not None and container.daily_worker._task is not None
        assert container.object_worker is not None and container.object_worker._task is not None
        assert container.topic_worker is not None and container.topic_worker._task is not None
        topic_service = container.topic_production_service
        topic_worker = container.topic_worker
        assert topic_worker._repository is topic_service._repository is topic_worker._workflow._repository
        assert topic_worker._service is topic_worker._workflow._service is topic_service
        assert topic_worker._poll_interval == container.memory_production_settings.memory_production_poll_seconds
        assert topic_service._repository._database is container.database
        assert topic_service._model._client is clients[1]
        assert topic_service._index_repository is container.memory_storage.get_index_repository()
        assert topic_service._retrieval._recall_repository is container.memory_storage.get_production_recall_repository()
        load.assert_called_once()
        assert topic_service._retrieval._rerank_repository is container.memory_storage.get_rerank_repository()
        assert [c.options["base_url"] for c in clients]==["https://glm.invalid","https://ds.invalid"]
        service=container.daily_production_service
        assert service._model_config.model_name=="glm-5.3"
        assert service._review_model_config.model_name=="deepseek-chat"
        daily_index = service._index_repository
        shared_index = container.memory_storage.get_index_repository()
        assert isinstance(daily_index, DailyIndexRepository)
        assert daily_index is not shared_index
        assert daily_index._client is shared_index._client
        assert daily_index._encoder is shared_index._encoder is encoder
        assert daily_index._encode_lock is shared_index._encode_lock
        assert daily_index._collection_lock is shared_index._collection_lock
        assert (daily_index._model_version, daily_index._batch_size, daily_index._max_length) == (
            shared_index._model_version, shared_index._batch_size, shared_index._max_length)
        object_service=container.object_production_service
        assert object_service._model_config.model_name=="deepseek-chat"
        assert object_service._model_config == ObjectModelConfig()
        assert object_service._model._client is clients[1]
        assert object_service._index_repository is shared_index
        edit_index = container.memory_edit_index_service._index_repository
        assert isinstance(edit_index, MemoryEditIndexRepository)
        assert edit_index._client is shared_index._client
        assert edit_index._encoder is shared_index._encoder
        assert edit_index._encode_lock is shared_index._encode_lock
        assert edit_index._collection_lock is shared_index._collection_lock
        assert container.memory_edit_worker._workflow._service is container.memory_edit_index_service
        trace=[]
        worker_stop=container.daily_worker.stop
        object_stop=container.object_worker.stop
        topic_stop=container.topic_worker.stop
        storage_stop=container.memory_storage.stop
        async def stop_worker():
            await worker_stop()
            trace.append("worker")
        async def stop_storage():
            trace.append("storage")
            await storage_stop()
        async def stop_object():
            await object_stop()
            trace.append("object-worker")
        async def stop_topic():
            await topic_stop()
            trace.append("topic-worker")
        monkeypatch.setattr(container.topic_worker,"stop",stop_topic)
        monkeypatch.setattr(container.daily_worker,"stop",stop_worker)
        monkeypatch.setattr(container.object_worker,"stop",stop_object)
        monkeypatch.setattr(container.memory_storage,"stop",stop_storage)
        for client in clients:
            client.close.side_effect=lambda: trace.append("client")
        await container.stop()
        assert trace.index("worker")<trace.index("client")<trace.index("storage")
        assert trace.index("topic-worker")<trace.index("object-worker")<trace.index("client")
        assert container.topic_worker._task is None
        assert container.daily_worker._task is container.object_worker._task is None
        assert container.database._closed
        assert all(client.close.await_count==1 for client in clients)
    finally:
        await container.stop()


@pytest.mark.asyncio
@pytest.mark.parametrize("production_enabled", [False, True])
async def test_recall_uses_storage_owned_connections(tmp_path,monkeypatch,production_enabled):
    encoder,load,clients=fake_resources(monkeypatch)
    cfg=settings(tmp_path,memory_enabled=True,memory_api_key="recall-test",
                 memory_summary_api_key="summary-test",
                 memory_production_enabled=production_enabled)
    cfg.memory_qdrant_path.mkdir()
    container=container_module.build_container(cfg)
    try:
        await container.start()
        shared=container.memory_storage.get_client()
        assert container.database is container.memory_storage.database
        recall = container.generation_preparation_service._memory_recall
        selection, summary = recall._model._client, recall._summary_service._gateway._client
        assert selection.options["base_url"] == summary.options["base_url"]
        assert selection.options["api_key"] == "recall-test"
        assert summary.options["api_key"] == "summary-test"
        readers=recall._readers
        reader=readers.create("c")
        assert reader._search._client is shared and reader._search._encoder is encoder
        other_reader = readers.create("c")
        assert other_reader is not reader
        assert other_reader._documents is not reader._documents
        assert other_reader._search._client is reader._search._client
        load.assert_called_once()
        assert reader._documents._database is container.database
        assert container.memory_storage.get_encoder() is encoder
    finally:
        await container.stop()
    assert container.database._closed
    assert container.memory_storage._qdrant is None
    assert all(client.close.await_count == 1 for client in clients)


@pytest.mark.asyncio
async def test_encoder_start_failure_releases_storage(tmp_path, monkeypatch):
    _, load, clients = fake_resources(monkeypatch)
    load.side_effect = RuntimeError("encoder failed")
    config = settings(tmp_path)
    container = container_module.build_container(config)
    with pytest.raises(RuntimeError, match="encoder failed"):
        await container.start()
    assert container.database._closed
    assert not clients
    reopened = memory_runtime.AsyncQdrantClient(path=str(config.memory_qdrant_path))
    await reopened.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("worker_class", [DailyWorker, ObjectWorker, TopicWorker])
async def test_worker_recovery_failure_aborts_start_and_closes_everything(tmp_path,monkeypatch,worker_class):
    _,_,clients=fake_resources(monkeypatch)
    monkeypatch.setattr(worker_class,"recover",AsyncMock(side_effect=RuntimeError("recover failed")))
    container=container_module.build_container(settings(tmp_path))
    with pytest.raises(RuntimeError,match="recover failed"):
        await container.start()
    assert container.daily_worker._task is None
    if container.object_worker is not None:
        assert container.object_worker._task is None
    if container.topic_worker is not None:
        assert container.topic_worker._task is None
    assert container.memory_storage._qdrant is None and container.database._closed
    assert all(client.close.await_count==1 for client in clients)


@pytest.mark.asyncio
async def test_memory_disabled_starts_sqlite_only(tmp_path):
    container=container_module.build_container(settings(tmp_path,memory_production_enabled=False))
    try:
        await container.start()
        assert container.database is container.memory_storage.database
        assert container.database.path.is_file()
        assert container.memory_storage._qdrant is None
        assert container.memory_production_settings is container.memory_edit_worker is None
        assert container.daily_worker is None
        assert container.object_worker is container.object_production_service is None
        assert container.topic_worker is container.topic_production_service is None
    finally:
        await container.stop()
    assert container.database._closed


@pytest.mark.parametrize("missing",["reply_api_key","default_api_key","memory_qdrant_path","memory_embedding_path"])
def test_production_requires_both_providers_and_storage_paths(tmp_path,missing):
    with pytest.raises(ValueError):
        container_module.build_container(settings(tmp_path,**{missing:None}))


@pytest.mark.asyncio
@pytest.mark.parametrize("interrupted", [False, True])
async def test_object_worker_scans_sealed_session_and_publishes_on_start(tmp_path, monkeypatch, interrupted):
    """应用真实启停、SQLite/Qdrant；模型和编码替身，不产生在线调用。"""
    import json
    from datetime import datetime, timezone
    import numpy as np
    from sqlalchemy import func, select
    from module.memory_management.shared.jobs.core.domain.command import CreateMemoryJob
    from module.memory_management.shared.jobs.core.domain.job import MemoryJobStatus
    from storage.sqlite.models import CharacterRow, ConversationRow, SessionRow, TurnRow, MessageRow, MemoryJobRow, MemoryObjectRow
    from module.memory_management.object.repository.sqlite import ObjectProductionRepository

    data = {"session_id": "s", "selection_notes": [], "objects": [{
        "object": "项目整理", "events": [], "plans": [{"actor": "user", "statement": "决定明天整理项目",
        "acquire_time": "2026-11-01", "target_time": "2026-11-02", "message_ids": ["u"]}], "forgetfulness": 0}]}
    response = SimpleNamespace(choices=[SimpleNamespace(finish_reason="stop", message=SimpleNamespace(content=json.dumps(data, ensure_ascii=False)))])
    _, load, clients = fake_resources(monkeypatch, object_response=response)
    def encode(self, texts):
        from qdrant_client import models
        return [{"dense": np.ones(1024).tolist(),
                 "sparse": models.SparseVector(indices=[1], values=[1.0])} for _ in texts]
    monkeypatch.setattr(QdrantMemoryIndexRepository, "_encode", encode)
    # 本测试专注object；daily并发启停由上面的生命周期测试覆盖。
    monkeypatch.setattr(DailyWorker, "process_next", AsyncMock(return_value=False))
    monkeypatch.setattr(TopicWorker, "process_next", AsyncMock(return_value=False))
    container = container_module.build_container(settings(tmp_path, memory_production_poll_seconds=0.01))
    try:
        await container.database.initialize()
        now = datetime(2026, 11, 1, 8, tzinfo=timezone.utc)
        async with container.database.transaction() as session:
            session.add(CharacterRow(character_id="c", name="c", character_setting="test"))
            await session.flush()
            session.add(ConversationRow(conversation_id="conv", character_id="c", state="archived",
                                       created_at=now, updated_at=now))
            await session.flush()
            session.add(SessionRow(session_id="s", conversation_id="conv", session_state="sealed",
                                   session_start_time=now, session_end_time=now))
            await session.flush()
            session.add(TurnRow(turn_id="t", session_id="s", turn_seq=1, turn_state="complete"))
            await session.flush()
            session.add_all([
                MessageRow(message_id="u", turn_id="t", message_seq=1, role="user", content="我决定明天整理项目", created_at=now),
                MessageRow(message_id="a", turn_id="t", message_seq=2, role="character", content="好，陪你一起", created_at=now),
            ])
            await session.flush()
            (await session.get(TurnRow, "t")).accepted_message_id="a"
        repository = ObjectProductionRepository(container.database)
        if interrupted:
            job_id = await repository.create_job(ObjectJobInput.create_command("c", "s"))
            started = await repository.begin_generate(job_id)
            assert started.status == MemoryJobStatus.GENERATING
        await container.start()
        async with asyncio.timeout(10):
            while True:
                async with container.database.db_session() as session:
                    row = await session.scalar(select(MemoryJobRow).where(MemoryJobRow.job_type == "extract_objects"))
                    if row is not None and row.status == MemoryJobStatus.DONE.value:
                        job_id = row.job_id
                        break
                await asyncio.sleep(0.01)
        done = await repository.get_job(job_id)
        assert done.finished_at is not None
        assert done.attempt_count == (3 if interrupted else 2)
        # 唤醒扫描后仍为同一任务，不重复生成或发布。
        container.object_worker.notify()
        assert await container.object_production_service.register_object_jobs() == ()
        assert await container.object_worker.process_next() is False
        async with container.database.db_session() as session:
            assert await session.scalar(select(func.count()).select_from(MemoryJobRow)) == 1
            assert await session.scalar(select(func.count()).select_from(MemoryObjectRow)) == 1
            row = await session.get(MemoryObjectRow, done.objects[0].object_id)
            assert row.object_state == "staged" and row.plans_json[0]["target_time"] == "2026-11-02"
        index = container.memory_storage.get_index_repository()
        assert (await container.memory_storage.get_client().count(index.object_collection_name)).count == 2
        load.assert_called_once()
        clients[1].chat.completions.create.assert_awaited_once()
        clients[0].chat.completions.create.assert_not_awaited()
        assert container.object_worker._task is not None and not container.object_worker._task.done()
    finally:
        await container.stop()
    assert container.database._closed and container.memory_storage._qdrant is None
    assert container.object_worker._task is None
    assert all(client.close.await_count == 1 for client in clients)


@pytest.mark.asyncio
async def test_topic_worker_recovers_before_polling(tmp_path, monkeypatch):
    fake_resources(monkeypatch)
    events = []
    original_recover = TopicWorker.recover
    async def recover(worker):
        await original_recover(worker)
        events.append("recovered")
    async def process(worker):
        assert events[0] == "recovered"
        events.append("polled")
        return False
    monkeypatch.setattr(TopicWorker, "recover", recover)
    monkeypatch.setattr(TopicWorker, "process_next", process)
    container = container_module.build_container(settings(tmp_path))
    try:
        await container.start()
        async with asyncio.timeout(5):
            while "polled" not in events:
                await asyncio.sleep(0.01)
        assert events.count("recovered") == 1
        assert not container.topic_worker._task.done()
    finally:
        await container.stop()
    assert container.topic_worker._task is None
