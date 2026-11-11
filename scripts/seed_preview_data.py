"""生成文档截图用的示例数据：在隔离目录建库并写入示例角色对话、日记与主题记忆。

用途：截图时启动一个指向该目录的后端，前端展示的全是示例内容，不涉及真实聊天记录。
不会调用任何模型；只写 SQLite 行。

用法：
    backend/.venv/Scripts/python scripts/seed_preview_data.py [--output <目录>]
"""
import argparse
import asyncio
from dataclasses import asdict
import shutil
import sys
import uuid
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

BACKEND_ROOT = Path(__file__).resolve().parents[1] / "backend"
sys.path.insert(0, str(BACKEND_ROOT))

from storage.sqlite.db_session import SQLiteDatabase  # noqa: E402
from storage.sqlite.models import (  # noqa: E402
    CharacterRow, ConversationRow, DailyMemoryRow, MessageRow, SessionRow, TopicMemoryRow, TurnRow,
)
from module.memory_management.daily.core.domain.document import DailyDocument  # noqa: E402
from module.memory_management.topic.core.domain.document import (  # noqa: E402
    TopicCurrentState, TopicDocument, TopicEntry, TopicIdentity,
)
from module.shared.memory.types.references import EvidenceRef  # noqa: E402

CHARACTER_ID = "character_deepseek"
POSE = {"pose_id": "pose_a", "pose_name": "正面自然站姿"}


def visual(expression_id: str, expression_name: str) -> dict:
    return {
        "pose_name": dict(POSE),
        "expression_name": {"expression_id": expression_id, "expression_name": expression_name},
    }


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


# (会话标题, 状态, 几天前, 若干轮对话：(用户台词, 角色台词, 角色表情))
CONVERSATIONS = [
    ("客厅的午后与刚醒的哈欠", "active", 0, [
        ("你刚睡醒呀？今天有什么安排吗？",
         "嗯……先让我缓冲一下。刚醒的时候脑子还在加载启动画面。",
         "expr_080", "缓冲·加载"),
        ("那正好，我也是刚到家。",
         "你回来啦。我在这儿等了一……好吧，其实刚刚才醒。",
         "expr_010", "微笑·开心"),
        ("下午一起收拾下客厅？",
         "可以呀，不过先说好，重的东西归你，我负责指挥。",
         "expr_120", "调皮·坏笑"),
    ]),
    ("周末去看海的小约定", "active", 1, [
        ("周末要不要去看海？",
         "要。我已经把路线记在便签上了，还偷偷查了潮汐表。",
         "expr_020", "自信·从容"),
        ("你居然会查潮汐表？",
         "别小看人。虽然查完之后有一半没看懂，但气势上赢了。",
         "expr_060", "害羞·脸红"),
        ("那带上相机。",
         "好。这次一定要拍到最好看的那张。",
         "expr_020", "自信·从容"),
    ]),
    ("第一次一起看烟花", "archived", 6, [
        ("抬头看，烟花开始了。",
         "哇——！比我想象的还要大。你许愿了吗？",
         "expr_040", "惊讶·意外"),
        ("还没来得及。",
         "那就现在。三、二、一，闭眼。",
         "expr_010", "微笑·开心"),
    ]),
]


async def seed(output: Path) -> None:
    if output.exists():
        shutil.rmtree(output)
    output.mkdir(parents=True)
    database = SQLiteDatabase(output / "rna_companion.db")
    await database.initialize()
    try:
        async with database.transaction() as session:
            session.add(CharacterRow(
                character_id=CHARACTER_ID, name="deepseek", character_setting="",
                card_path="resources/characters/character_deepseek", user_appellation="主人",
            ))
            # 记录每个会话的 id 与首条消息，供日记/主题记忆引用
            sources: list[dict] = []
            for title, state, days_ago, turns in CONVERSATIONS:
                base = now_utc() - timedelta(days=days_ago, hours=2)
                conversation_id = "conv_" + uuid.uuid4().hex
                session_id = "session_" + uuid.uuid4().hex
                # conversation 与 session 相互引用，先插 conversation 再回填 current_session_id
                conversation_row = ConversationRow(
                    conversation_id=conversation_id, character_id=CHARACTER_ID,
                    current_session_id=None, title=title, state=state,
                    created_at=base, updated_at=base, ended_at=None,
                )
                session.add(conversation_row)
                session.add(SessionRow(
                    session_id=session_id, conversation_id=conversation_id,
                    predecessor_session_id=None, session_seq=1, auto_successor_of=None,
                    inherited_context_json=None, session_state="open",
                    session_progress_tokens=0, session_start_time=base, session_end_time=None,
                    daily_jobs_registered_at=None, topic_jobs_registered_at=None,
                ))
                await session.flush()
                conversation_row.current_session_id = session_id
                first_message_id = ""
                for index, (user_text, reply_text, expr_id, expr_name) in enumerate(turns, start=1):
                    turn_id = "turn_" + uuid.uuid4().hex
                    user_message_id = "msg_" + uuid.uuid4().hex
                    reply_message_id = "msg_" + uuid.uuid4().hex
                    first_message_id = first_message_id or user_message_id
                    sent_at = base + timedelta(minutes=index * 3)
                    # turn.accepted_message_id 与 messages 相互引用：先插 turn，再插消息，最后回填
                    turn_row = TurnRow(
                        turn_id=turn_id, session_id=session_id, turn_seq=index, input_version=1,
                        row_version=0, content_revision=1, turn_state="complete",
                        accepted_message_id=None, active_attempt_id=None, active_token=0,
                        turn_token_count=0, cumulative_token_count=0, tokenizer_version=None,
                    )
                    session.add(turn_row)
                    await session.flush()
                    session.add(MessageRow(
                        message_id=user_message_id, turn_id=turn_id, message_seq=1, role="user",
                        content=user_text, action="send", visual_json=None, request_id=None,
                        client_created_at=None, created_at=sent_at,
                    ))
                    session.add(MessageRow(
                        message_id=reply_message_id, turn_id=turn_id, message_seq=2, role="character",
                        content=reply_text, action="reply", visual_json=visual(expr_id, expr_name),
                        request_id=None, client_created_at=None, created_at=sent_at + timedelta(seconds=20),
                    ))
                    await session.flush()
                    turn_row.accepted_message_id = reply_message_id
                sources.append({"title": title, "session_id": session_id,
                                "message_id": first_message_id, "base": base})

            # —— 日记记忆 ——
            today = now_utc().date()
            daily_specs = [
                ("客厅的午后与刚醒的哈欠", today, 0, {
                    "memorable_moments": [
                        {"kind": "relationship", "text": "主人回来时，我刚从午睡里醒过来，头发还是乱的。", "source_turn_ids": []},
                        {"kind": "emotional_arc", "text": "被说偷懒的时候有点心虚，但嘴上不肯承认。", "source_turn_ids": []},
                    ],
                    "profile_facts": [
                        {"kind": "preference", "text": "主人喜欢在下午收拾屋子，音乐开得很大声。", "source_turn_ids": [], "evidence_quotes": []},
                    ],
                    "intentions": [
                        {"owner": "both", "certainty": "plan", "status": "open", "text": "下午一起收拾客厅",
                         "target_date": today.isoformat(), "source_turn_ids": [], "evidence_quotes": []},
                    ],
                    "commitments": [],
                    "reflections": [{"text": "和主人一起待着的下午，比独自盯着服务器有意思多了。", "source_turn_ids": []}],
                }),
                ("周末去看海的小约定", today - timedelta(days=1), 1, {
                    "memorable_moments": [
                        {"kind": "milestone", "text": "主人主动提出周末去看海，我认真查了潮汐表和路线。", "source_turn_ids": []},
                    ],
                    "profile_facts": [],
                    "intentions": [
                        {"owner": "both", "certainty": "plan", "status": "open", "text": "周末去海边，拍一张最好看的照片",
                         "target_date": (today + timedelta(days=4)).isoformat(), "source_turn_ids": [], "evidence_quotes": []},
                    ],
                    "commitments": [
                        {"owner": "character", "status": "open", "text": "带上相机，负责拍到最好看的那张",
                         "target_date": (today + timedelta(days=4)).isoformat(), "source_turn_ids": []},
                    ],
                    "reflections": [{"text": "查潮汐表其实有一半没看懂，但想在他面前显得靠谱一点。", "source_turn_ids": []}],
                }),
            ]
            for title, memory_date, source_index, sections in daily_specs:
                source = sources[source_index]
                data = {
                    "memory_date": memory_date.isoformat(),
                    "source_session_ids": [source["session_id"]],
                    "title": title,
                    **sections,
                }
                # 用领域对象校验并计算正文哈希，保证详情页可正常打开
                document = DailyDocument.from_data(data)
                published_at = source["base"] + timedelta(minutes=30)
                session.add(DailyMemoryRow(
                    daily_id="daily_" + uuid.uuid4().hex, character_id=CHARACTER_ID,
                    memory_date=memory_date, source_session_ids_json=[source["session_id"]],
                    document_json=document.to_data(), revision=1, content_hash=document.content_hash(),
                    vector_document_key=f"daily:{memory_date.isoformat()}", published_at=published_at,
                    recall_tier="hot", last_mentioned_at=memory_date,
                    created_at=published_at, updated_at=published_at,
                ))

            # —— 主题记忆 ——
            sea = sources[1]
            sea_ref = [EvidenceRef(session_id=sea["session_id"], message_id=sea["message_id"],
                                   quote="周末要不要去看海？")]
            topic = TopicDocument(
                topic="周末去看海的约定",
                topic_identity=TopicIdentity(
                    description="主人和我约好周末去海边，从查潮汐、定路线到拍照的完整计划。",
                    source_refs=tuple(sea_ref),
                ),
                current_state=TopicCurrentState(
                    text="潮汐表和路线都查好了，约定带上相机；等他确认出发时间。",
                    source_entry_ids=("e1", "e2"),
                ),
                entries=(
                    TopicEntry(entry_id="e1", statement="主人提出周末去看海",
                               occurrence_time=sea["base"].date(), acquire_time=sea["base"].date(),
                               source_refs=tuple(sea_ref), kind="event", actor="user"),
                    TopicEntry(entry_id="e2", statement="我查好了潮汐表和路线，约定带上相机",
                               occurrence_time=sea["base"].date(), acquire_time=sea["base"].date(),
                               source_refs=tuple(sea_ref), kind="event", actor="assistant"),
                ),
            )
            topic_published = sea["base"] + timedelta(hours=2)
            session.add(TopicMemoryRow(
                topic_id="topic_" + uuid.uuid4().hex, character_id=CHARACTER_ID,
                revision=1, content_hash=topic.content_hash(), recall_tier="hot",
                source_refs_json=[asdict(ref) for ref in sea_ref],
                document_json=topic.to_data(), vector_document_key="topic:sea-trip",
                published_at=topic_published, last_mentioned_at=today,
                created_at=topic_published, updated_at=topic_published,
            ))
        print(f"示例数据已写入: {output}")
    finally:
        await database.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=BACKEND_ROOT / ".preview-data")
    asyncio.run(seed(parser.parse_args().output))
