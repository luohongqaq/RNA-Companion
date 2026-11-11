"""正式topic正文、版本、时间线映射和非破坏性迁移。"""
from dataclasses import replace
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from module.shared.common.errors.base import InvalidStateError
from module.memory_management.shared.core.domain.document import TopicMemory
from module.shared.memory.types.references import EvidenceRef, MemoryKind
from module.shared.memory.types.retention import RecallTier
from module.memory_management.topic.core.domain.document import TopicDocument, TopicIdentity, TopicCurrentState, TopicEntry
from module.memory_management.shared.repository import document_mappers
from storage.sqlite.db_session import SQLiteDatabase
from storage.sqlite.models import CharacterRow, TopicMemoryRow


def memory():
    now=datetime(2026,11,1,8,tzinfo=timezone.utc)
    source=EvidenceRef("s", "m", "原文证据", "turn", 2, "source-hash")
    doc=TopicDocument("桌宠项目", TopicIdentity("用户自己的桌面陪伴项目", (source,)),
        TopicCurrentState("已建立项目，打算下周补动作", ("corrected","plan")), (
            TopicEntry("old", "此前认为未建立", None, date(2026,10,30), (source,)),
            TopicEntry("corrected", "更正：项目已经建立", date(2026,10,31), date(2026,11,1), (source,), corrects_entry_id="old"),
            TopicEntry("plan", "计划下周补动作", None, date(2026,11,1), (source,), kind="plan", target_time=date(2026,11,8)),
        ))
    return TopicMemory(document_id="topic",kind=MemoryKind.TOPIC,current_revision=2,content_hash="hash",
        vector_document_key="topic:r2:hash",published_at=now,updated_at=now,source_refs=(source,),document=doc)


@pytest_asyncio.fixture
async def database(tmp_path):
    db=SQLiteDatabase(tmp_path/"topic.sqlite3")
    await db.initialize()
    try:
        async with db.transaction() as session:
            session.add(CharacterRow(character_id="c",name="c",character_setting="test"))
        yield db
    finally:
        await db.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("tier", list(RecallTier))
async def test_formal_topic_roundtrip_preserves_document_and_versions(database,tier):
    value=memory()
    created=value.published_at-timedelta(days=7)
    async with database.transaction() as session:
        session.add(document_mappers.topic_memory_to_row(value,character_id="c",recall_tier=tier,created_at=created))
    async with database.db_session() as session:
        row=await session.get(TopicMemoryRow,"topic")
        assert document_mappers.topic_memory_to_domain(row)==value
        assert row.recall_tier==tier.value and row.created_at==created
        assert row.document_json["topic_identity"]["description"]==value.document.topic_identity.description
        events=row.document_json["entries"]
        assert events[0]["occurrence_time"] is None
        assert events[1]["corrects_entry_id"]=="old"
        assert events[1]["acquire_time"]=="2026-11-01" and events[1]["occurrence_time"]=="2026-10-31"
        assert events[2]["kind"]=="plan" and events[2]["target_time"]=="2026-11-08"
        assert row.source_refs_json[0]["source_revision"]==2
        assert row.source_refs_json[0]["quote"]=="原文证据"
        assert type(document_mappers.topic_memory_to_domain(row).document.entries[0].acquire_time) is date


@pytest.mark.asyncio
async def test_topic_version_signals_ignore_export_path_and_follow_vector_reference(database):
    async with database.transaction() as session:
        session.add(document_mappers.topic_memory_to_row(memory(),character_id="c"))
    async with database.db_session() as session:
        before=await session.scalar(text("SELECT seq FROM memory_update_clock"))
        assert await session.scalar(text("SELECT kind FROM memory_update_signals WHERE document_id='topic'"))=="topic"
    async with database.db_session() as session:
        assert await session.scalar(text("SELECT seq FROM memory_update_clock"))==before
    async with database.transaction() as session:
        (await session.get(TopicMemoryRow,"topic")).vector_document_key="new-reference"
    async with database.db_session() as session:
        assert await session.scalar(text("SELECT seq FROM memory_update_clock"))==before+1
    async with database.transaction() as session:
        row=await session.get(TopicMemoryRow,"topic")
        row.revision,row.content_hash=3,"new-hash"
    async with database.db_session() as session:
        assert await session.scalar(text("SELECT revision FROM memory_update_signals WHERE document_id='topic'"))==3
    async with database.transaction() as session:
        await session.delete(await session.get(TopicMemoryRow,"topic"))
    async with database.db_session() as session:
        assert await session.scalar(text("SELECT deleted FROM memory_update_signals WHERE document_id='topic'"))==1


@pytest.mark.asyncio
async def test_topic_primary_key_and_character_fk(database):
    value=memory()
    async with database.transaction() as session:
        session.add(document_mappers.topic_memory_to_row(value,character_id="c"))
    with pytest.raises(IntegrityError):
        async with database.transaction() as session:
            session.add(document_mappers.topic_memory_to_row(value,character_id="c"))
    with pytest.raises(IntegrityError):
        async with database.transaction() as session:
            session.add(document_mappers.topic_memory_to_row(replace(value,document_id="other"),character_id="missing"))
    with pytest.raises(ValueError):
        document_mappers.topic_memory_to_row(replace(value,kind=MemoryKind.DAILY),character_id="c")


@pytest.mark.parametrize("field", ["document_json","vector_document_key","published_at"])
def test_metadata_only_topic_is_not_read_as_a_complete_document(field):
    row=document_mappers.topic_memory_to_row(memory(),character_id="c")
    setattr(row,field,None)
    with pytest.raises(InvalidStateError,match="正式topic缺少"):
        document_mappers.topic_memory_to_domain(row)


@pytest.mark.asyncio
async def test_reinitialize_preserves_incomplete_topic_metadata(tmp_path):
    db=SQLiteDatabase(tmp_path/"legacy.sqlite3")
    try:
        await db.initialize()
        async with db.transaction() as session:
            await session.execute(text("INSERT INTO characters(character_id,name,character_setting) VALUES ('c','c','test')"))
            await session.execute(text("""INSERT INTO topic_memories(topic_id,character_id,revision,content_hash,recall_tier,source_refs_json,created_at,updated_at)
                VALUES ('legacy','c',3,'old-hash','cold','[]','2026-10-30T08:00:00+00:00','2026-10-31T08:00:00+00:00')"""))
        await db.initialize()
        await db.initialize()
        async with db.db_session() as session:
            row=await session.get(TopicMemoryRow,"legacy")
            assert (row.revision,row.content_hash,row.recall_tier)==(3,"old-hash","cold")
            assert row.document_json is row.vector_document_key is row.published_at is None
            assert not (await session.execute(text("PRAGMA foreign_key_check"))).all()
            assert await session.scalar(text("PRAGMA integrity_check"))=="ok"
            assert (await session.scalars(text("SELECT version FROM schema_migrations ORDER BY version"))).all()==[1, 2, 3, 4, 5, 6, 7]
            with pytest.raises(InvalidStateError):
                document_mappers.topic_memory_to_domain(row)
    finally:
        await db.close()



def test_topic_write_markdown_keeps_dates_and_corrections_but_hides_evidence():
    from module.memory_management.topic.core.domain.publication import TopicWrite
    document=memory().document
    result=TopicWrite.from_document(document)
    assert result.document is document
    assert result.document.render_text()==document.render_text()
    assert result.content_hash==document.content_hash()
    assert result.document.render_text().startswith("# 桌宠项目\n") and result.document.render_text().endswith("\n")
    assert "## 事件主题说明" in result.document.render_text() and "## 当前状态" in result.document.render_text() and "## 时间线" in result.document.render_text()
    assert "2026-11-01 得知" in result.document.render_text()
    assert "发生日期：2026-10-31" in result.document.render_text()
    assert "目标日期：2026-11-08" in result.document.render_text()
    assert "用户计划：计划下周补动作" in result.document.render_text()
    assert "〔已被更正〕用户：此前认为未建立" in result.document.render_text()
    assert "〔更正〕用户：更正：项目已经建立" in result.document.render_text()
    assert all(secret not in result.document.render_text() for secret in ("原文证据", "source-hash", "corrected", "source_refs", "entry_id"))
    # 未知发生日保持未知，不能用得知日补成发生日。
    first=result.document.render_text().split("## 时间线\n\n",1)[1].splitlines()[0]
    assert "发生日期" not in first


def test_topic_three_index_sections_preserve_semantics_without_metadata():
    import hashlib
    from module.memory_management.topic.core.domain.publication import TopicWrite
    from module.shared.memory.types.references import MemoryRevisionRef
    from module.shared.memory.types.indexing import RepresentationKind
    result=TopicWrite.from_document(memory().document)
    ref=MemoryRevisionRef(MemoryKind.TOPIC,"topic",2,result.content_hash)
    sections=result.index_representations(ref,"c",RecallTier.COLD)
    assert [s.representation_kind for s in sections]==[
        RepresentationKind.TOPIC_NAME,RepresentationKind.TOPIC_IDENTITY,RepresentationKind.TOPIC_CURRENT]
    assert [s.section_key for s in sections]==["topic_name","topic_identity","topic_current"]
    for section in sections:
        assert section.text == "桌宠项目" or section.text.startswith("桌宠项目\n")
        assert section.ref==ref and section.character_id=="c" and section.recall_tier==RecallTier.COLD
        assert section.text_hash==hashlib.sha256(section.text.encode("utf-8")).hexdigest()
        assert all(secret not in section.text for secret in ("原文证据", "source-hash", "2026-", "得知｜"))
    assert "计划下周补动作" not in sections[0].text
    assert all("〔已被更正〕" not in section.text and "用户计划" not in section.text for section in sections)
    with pytest.raises(ValueError):
        result.index_representations(replace(ref,content_hash="wrong"),"c",RecallTier.HOT)
    with pytest.raises(ValueError):
        result.index_representations(replace(ref,kind=MemoryKind.DAILY),"c",RecallTier.HOT)


def test_topic_hash_covers_structured_dates_and_evidence_without_leaking_to_public_text():
    from module.memory_management.topic.core.domain.publication import TopicWrite
    document=memory().document
    same=replace(document)
    assert same.content_hash()==document.content_hash()
    changed_event=replace(document.entries[0],source_refs=(replace(document.entries[0].source_refs[0],quote="不同证据"),))
    evidence_changed=replace(document,entries=(changed_event,*document.entries[1:]))
    assert evidence_changed.render_text()==document.render_text()
    assert evidence_changed.content_hash()!=document.content_hash()
    changed_date=replace(document,entries=(*document.entries[:2],replace(document.entries[2],target_time=date(2026,11,9))))
    assert changed_date.content_hash()!=document.content_hash()
    assert changed_date.render_text()!=document.render_text()
    payload=document.public_payload()
    assert set(payload)=={"title","topic_identity","current_state","entries"}
    assert "原文证据" not in str(payload) and "目标日期：2026-11-08" in payload["entries"]
    empty=replace(document,topic_identity=TopicIdentity("",()),current_state=TopicCurrentState("",()),entries=())
    result=TopicWrite.from_document(empty)
    from module.shared.memory.types.references import MemoryRevisionRef
    ref=MemoryRevisionRef(MemoryKind.TOPIC,"empty",1,result.content_hash)
    assert [item.text for item in result.index_representations(ref,"c",RecallTier.HOT)] == ["桌宠项目"]
    assert result.document.render_text()=="# 桌宠项目\n"
