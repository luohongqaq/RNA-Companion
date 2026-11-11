"""生成事务仓储：T2-A领取、T2-B冻结、候选保存及T3正式采用。"""
from logger import stage

from datetime import datetime, timezone
import hashlib
import json
from dataclasses import replace

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from module.chat.conversation.core.domain.models import ConversationState, SessionState, TurnState
from module.shared.common.ids import IdGenerator
from module.shared.common.errors.base import InvalidStateError, NotFoundError
from module.context_compaction.core.domain.lifecycle_policy import SessionLifecycleConfig
from module.context_compaction.core.domain.lifecycle import LifecycleAction
from module.context_compaction.core.domain.job import ContextJobType, CreateContextJobCommand
from module.chat.reply.core.domain.fingerprint import ReplyFingerprint
from module.shared.context.types.references import InheritedContextRef, SnapshotRef
from module.context_compaction.core.domain.snapshots import SnapShotState
from module.chat.reply.core.domain.contracts import (
    AcceptedReply, GenerationFreezeTicket, GenerationStartTicket, ReplyAcceptanceTicket,
)
from module.chat.reply.core.domain.materials import GenerationInputBoundary, PromptMessage, ReplyCandidate
from module.chat.reply.core.domain.models import AttemptState, GenerationAttempt, GenerationFailure, GenerationJob, GenerationJobStatus
from module.shared.jobs.config.retry import RetryPolicy
from module.context_compaction.repository import mappers as context_mappers
from module.shared.common.time import BackendTime
from module.memory_management.retention.repository.sqlite import SQLiteMemoryRetentionRepository
from storage.sqlite.value_codecs import SQLiteValueCodec
from module.chat.reply.repository import mappers as reply_mappers
from module.chat.conversation.repository import mappers as conversation_mappers
from storage.sqlite.db_session import SQLiteDatabase
from module.context_compaction.repository.sqlite import SQLiteContextMaintenanceRepository
from storage.sqlite.models import (
    ContextSnapshotRow, ConversationRow, GenerationAttemptRow, GenerationJobRow,
    MessageRow, SessionRow, TurnRow, ChatEventRow,
)


class SQLiteGenerationRepository:
    def __init__(self, database: SQLiteDatabase, *, lifecycle_policy: SessionLifecycleConfig | None = None) -> None:
        self._database = database
        self._lifecycle_policy = SessionLifecycleConfig() if lifecycle_policy is None else lifecycle_policy
        self._context_repository = SQLiteContextMaintenanceRepository(database, lifecycle_policy=self._lifecycle_policy)

    async def list_ready_jobs(self, limit: int = 32) -> tuple[GenerationJob, ...]:
        if not 1 <= limit <= 256:
            raise ValueError("任务扫描数量必须在1到256之间")
        async with self._database.db_session() as db:
            rows = (await db.scalars(select(GenerationJobRow).where(
                GenerationJobRow.state.in_(("need_prepare", "need_generate", "need_accept")),
                GenerationJobRow.available_at <= datetime.now(timezone.utc),
            ).order_by(GenerationJobRow.priority, GenerationJobRow.created_at, GenerationJobRow.job_id).limit(limit))).all()
            return tuple(reply_mappers.generation_job_to_domain(row) for row in rows)

    async def get_job(self, job_id: str) -> GenerationJob | None:
        async with self._database.db_session() as db:
            row = await db.get(GenerationJobRow, job_id)
            return reply_mappers.generation_job_to_domain(row) if row else None

    async def list_conversation_jobs(self, conversation_id: str) -> tuple[GenerationJob, ...] | None:
        async with self._database.db_session() as db:
            if await db.get(ConversationRow, conversation_id) is None:
                return None
            stmt = select(GenerationJobRow).join(
                TurnRow, GenerationJobRow.turn_id == TurnRow.turn_id,
            ).join(SessionRow, TurnRow.session_id == SessionRow.session_id).join(
                ConversationRow, SessionRow.conversation_id == ConversationRow.conversation_id,
            ).where(
                ConversationRow.conversation_id == conversation_id,
                ConversationRow.current_session_id == SessionRow.session_id,
                TurnRow.turn_state == TurnState.CURRENT.value,
                TurnRow.accepted_message_id.is_(None),
                GenerationJobRow.expected_input_version == TurnRow.input_version,
                GenerationJobRow.state.in_(("need_prepare", "need_generate", "need_accept", "failed")),
            ).order_by(GenerationJobRow.created_at, GenerationJobRow.job_id)
            rows = (await db.scalars(stmt)).all()
            return tuple(reply_mappers.generation_job_to_domain(row) for row in rows)

    @stage("generation.retry.queue")
    @stage("generation.cancel.persist")
    async def cancel_job(self, job_id: str) -> GenerationJob:
        now = datetime.now(timezone.utc)
        async with self._database.transaction() as db:
            job = await db.get(GenerationJobRow, job_id)
            if job is None:
                raise NotFoundError("生成任务不存在")
            if job.state == GenerationJobStatus.CANCELLED.value:
                return reply_mappers.generation_job_to_domain(job)
            if job.state not in ("need_prepare", "need_generate", "need_accept"):
                raise InvalidStateError("只允许取消处理中的生成任务")
            turn = await db.get(TurnRow, job.turn_id)
            session = await db.get(SessionRow, turn.session_id) if turn else None
            conversation = await db.get(ConversationRow, session.conversation_id) if session else None
            if (turn is None or turn.turn_state != TurnState.CURRENT.value
                    or turn.accepted_message_id is not None
                    or turn.input_version != job.expected_input_version
                    or session is None or session.session_state != SessionState.OPEN.value
                    or conversation is None or conversation.current_session_id != session.session_id):
                raise InvalidStateError("该任务的输入或会话已失效，不能取消")
            if job.current_attempt_id is not None:
                attempt = await db.get(GenerationAttemptRow, job.current_attempt_id)
                if (attempt is None or attempt.job_id != job.job_id or attempt.turn_id != turn.turn_id
                        or turn.active_attempt_id != attempt.attempt_id
                        or attempt.input_version != job.expected_input_version
                        or turn.active_token != attempt.active_token
                        or attempt.attempt_state not in ("retrieving", "generating")):
                    raise InvalidStateError("当前生成尝试已失效，不能取消")
                attempt.attempt_state = AttemptState.SUPERSEDED.value
                attempt.completed_at = attempt.updated_at = now
            job.state = GenerationJobStatus.CANCELLED.value
            job.finished_at = job.updated_at = now
            job.failure_json = None
            db.add(ChatEventRow(
                event_type="generation_cancelled", conversation_id=session.conversation_id,
                job_id=job.job_id, manual_retry_count=job.manual_retry_count, created_at=now,
                cancel_json={"conversation_id": session.conversation_id, "turn_id": job.turn_id,
                             "input_version": job.expected_input_version,
                             "manual_retry_count": job.manual_retry_count},
            ))
            await db.flush()
            return reply_mappers.generation_job_to_domain(job)

    async def retry_failed_job(self, job_id: str) -> GenerationJob:
        """重试失败的生成任务，复用旧生成的材料"""
        now = datetime.now(timezone.utc)
        async with self._database.transaction() as db:
            job = await db.get(GenerationJobRow, job_id)
            if job is None:
                raise NotFoundError("生成任务不存在")
            turn = await db.get(TurnRow, job.turn_id)
            session = await db.get(SessionRow, turn.session_id) if turn else None
            conversation = await db.get(ConversationRow, session.conversation_id) if session else None
            # 先检查任务是否还能重试
            if (turn is None or turn.turn_state != TurnState.CURRENT.value
                    or turn.accepted_message_id is not None
                    or turn.input_version != job.expected_input_version):
                raise InvalidStateError("该任务的输入已失效，不能重试")
            if (session is None or conversation is None
                    or session.session_state != SessionState.OPEN.value
                    or conversation.state != ConversationState.ACTIVE.value
                    or conversation.current_session_id != session.session_id):
                raise InvalidStateError("当前会话不允许重试")
            if job.state != GenerationJobStatus.FAILED.value:
                if job.manual_retry_count > 0 and job.state in ("need_prepare", "need_generate", "need_accept"):
                    return reply_mappers.generation_job_to_domain(job)
                raise InvalidStateError("只允许重试失败的生成任务")
            # 创建新采用资格，但可复制已校验的昂贵阶段产物。
            reusable = None
            old_attempt = await db.get(GenerationAttemptRow, job.current_attempt_id) if job.current_attempt_id else None
            if (old_attempt is not None and old_attempt.job_id == job_id
                    and old_attempt.turn_id == turn.turn_id
                    and old_attempt.input_version == turn.input_version
                    and old_attempt.active_token == turn.active_token
                    and turn.active_attempt_id == old_attempt.attempt_id
                    and old_attempt.frozen_prompt_json is not None):
                try:
                    candidate = reply_mappers.generation_attempt_to_domain(old_attempt).candidate
                    ticket = ReplyAcceptanceTicket(
                        old_attempt.attempt_id, old_attempt.input_version, old_attempt.active_token,
                        old_attempt.prompt_hash, candidate.candidate_hash if candidate else "",
                    )
                    reusable = self._checked_materials(old_attempt, ticket)
                    boundary = reusable.input_boundary
                    ids = tuple((await db.scalars(select(MessageRow.message_id).where(
                        MessageRow.turn_id == turn.turn_id, MessageRow.role == "user",
                    ).order_by(MessageRow.message_seq))).all())
                    if (boundary.conversation_id != conversation.conversation_id
                            or boundary.character_id != conversation.character_id
                            or boundary.session_id != session.session_id
                            or boundary.current_turn.turn_id != turn.turn_id
                            or boundary.current_turn.input_version != turn.input_version
                            or boundary.current_turn.message_ids != ids):
                        reusable = None
                    elif candidate is not None:
                        try:
                            self._validate_candidate(ticket, candidate)
                        except InvalidStateError:
                            reusable = replace(reusable, candidate=None)
                except (InvalidStateError, ValueError, KeyError, TypeError):
                    # 损坏或不一致的产物不能复用，重新准备也不能采用旧候选。
                    reusable = None
            attempts = (await db.scalars(select(GenerationAttemptRow).where(
                GenerationAttemptRow.job_id == job_id,
                GenerationAttemptRow.attempt_state.in_(("retrieving", "generating")),
            ))).all()
            for attempt in attempts:
                attempt.attempt_state, attempt.updated_at = "superseded", now
            turn.active_token += 1
            turn.row_version += 1
            turn.active_attempt_id = None
            job.current_attempt_id = None
            job.state = GenerationJobStatus.NEED_PREPARE.value
            if reusable is not None:
                # 新建attempt_id，逐一复用旧的attempt的中间过程
                attempt_id = IdGenerator.new("attempt")
                last_seq = await db.scalar(select(func.max(GenerationAttemptRow.attempt_version)).where(
                    GenerationAttemptRow.turn_id == turn.turn_id,
                )) or 0
                audit = replace(reusable.memory_audit, attempt_id=attempt_id) if reusable.memory_audit else None
                # 基于旧attempt创建一个“恢复后的attempt”，并添加到数据库中
                resumed = replace(
                    reusable, attempt_id=attempt_id, attempt_seq=last_seq + 1,
                    active_token=turn.active_token, state=AttemptState.GENERATING,
                    created_at=now, updated_at=now, failure=None, finished_at=None, memory_audit=audit,
                )
                db.add(reply_mappers.generation_attempt_to_row(resumed, prompt_hash=old_attempt.prompt_hash))
                await db.flush()
                # 复用旧的attempt的中间过程
                job.current_attempt_id = turn.active_attempt_id = attempt_id
                job.attempt_count += 1
                job.state = (GenerationJobStatus.NEED_ACCEPT.value if resumed.candidate is not None
                             else GenerationJobStatus.NEED_GENERATE.value)
            job.retry_count, job.failure_json, job.finished_at = 0, None, None
            job.available_at, job.updated_at = now, now
            job.manual_retry_count += 1
            await db.flush()
            result = reply_mappers.generation_job_to_domain(job)
        return result

    async def resume_preparation(self, job_id: str) -> GenerationStartTicket | None:
        now = datetime.now(timezone.utc)
        async with self._database.transaction() as db:
            job = await db.get(GenerationJobRow, job_id)
            if (job is None or job.state != 'need_prepare' or job.available_at > now
                    or job.current_attempt_id is None):
                return None
            attempt = await db.get(GenerationAttemptRow, job.current_attempt_id)
            if attempt is None or attempt.attempt_state != 'retrieving':
                raise InvalidStateError("准备阶段缺少有效的retrieving attempt")
            if await self._eligible(db, attempt.attempt_id, attempt.input_version, attempt.active_token, now) is None:
                return None
            return GenerationStartTicket(job_id, attempt.attempt_id, attempt.turn_id,
                attempt.input_version, attempt.active_token, reply_mappers.boundary_from_json(attempt.input_boundary_json))

    async def check_attempt(self, attempt_id: str) -> bool:
        async with self._database.transaction() as db:
            row = await db.get(GenerationAttemptRow, attempt_id)
            return row is not None and await self._eligible(db, row.attempt_id, row.input_version,
                row.active_token, datetime.now(timezone.utc)) is not None

    async def record_failure(self, job_id: str, attempt_id: str | None,
                             failure: GenerationFailure, policy: RetryPolicy) -> bool:
        now = datetime.now(timezone.utc)
        async with self._database.transaction() as db:
            job = await db.get(GenerationJobRow, job_id)
            if (job is None or job.state not in ('need_prepare', 'need_generate', 'need_accept')
                    or job.current_attempt_id != attempt_id):
                return False
            attempt = None
            if attempt_id is not None:
                attempt = await db.get(GenerationAttemptRow, attempt_id)
                if attempt is None or await self._eligible(db, attempt_id, attempt.input_version,
                        attempt.active_token, now, verify_boundary=False) is None:
                    return False
            retry = failure.retryable and job.retry_count < policy.max_retries
            delay_index = min(job.retry_count, len(policy.delays) - 1)
            job.retry_count += 1
            job.failure_json = conversation_mappers.failure_to_json(failure)
            job.updated_at = now
            if retry:
                job.available_at = now + policy.delays[delay_index]
            else:
                job.state, job.finished_at = 'failed', now
                turn = await db.get(TurnRow, job.turn_id)
                session = await db.get(SessionRow, turn.session_id)
                db.add(ChatEventRow(
                    event_type="generation_failed", conversation_id=session.conversation_id,
                    job_id=job.job_id, manual_retry_count=job.manual_retry_count, created_at=now,
                    failure_json={"conversation_id": session.conversation_id,
                        "turn_id": job.turn_id, "input_version": job.expected_input_version,
                        "manual_retry_count": job.manual_retry_count, "error": job.failure_json},
                ))
            if attempt is not None:
                attempt.failure_json, attempt.updated_at = job.failure_json, now
                if not retry:
                    attempt.attempt_state, attempt.completed_at = 'failed', now
            await db.flush()
            return True

    @stage("generation.claim")
    async def claim_job(self, job_id: str) -> GenerationStartTicket | None:
        """T2-A入口：按任务门→数据库互斥→轮次资格→会话路由四道检查领取任务；
        通过后固定输入边界（有效快照、前驱继承交接、当前轮全部消息），创建retrieving
        attempt并推进job与turn的attempt及token状态，返回启动票据；
        资格不符时不写库或在同一事务内进入终态。

        输入:
        job_id
        输出:
        GenerationStartTicket: {job_id, attempt_id, turn_id, input_version, active_token,
                                input_boundary: GenerationInputBoundary}|None
        """
        now = datetime.now(timezone.utc)
        async with self._database.transaction() as db_session:
            # 任务门：不存在、已推进到后续阶段或还在退避期，都不领取也不写库
            job_row = await db_session.get(GenerationJobRow, job_id)
            if job_row is None:
                return None
            if job_row.state != GenerationJobStatus.NEED_PREPARE.value:
                return None
            if job_row.available_at > now:
                return None
            # 数据库级互斥：运行时attempt的存在说明已有人持有本任务
            live_attempt_id = await db_session.scalar(
                select(GenerationAttemptRow.attempt_id).where(
                    GenerationAttemptRow.job_id == job_id,
                    GenerationAttemptRow.attempt_state.in_((
                        AttemptState.RETRIEVING.value, AttemptState.GENERATING.value,
                    )),
                )
            )
            if live_attempt_id is not None:
                return None

            # 轮次资格：turn缺失、已完成或输入版本漂移,说明当前输入无效,该任务作废
            turn_row = await db_session.get(TurnRow, job_row.turn_id)
            if (
                turn_row is None
                or turn_row.turn_state != TurnState.CURRENT.value
                or turn_row.accepted_message_id is not None
                or turn_row.input_version != job_row.expected_input_version
            ):
                await self._retire(db_session, job_row, GenerationJobStatus.OBSOLETE, now)
                return None

            # 会话路由：对话归档或session封存属于主动取消，不是过时
            session_row = await db_session.get(SessionRow, turn_row.session_id)
            conversation_row = await db_session.get(ConversationRow, session_row.conversation_id)
            if (
                conversation_row.state != ConversationState.ACTIVE.value
                or session_row.session_state != SessionState.OPEN.value
            ):
                await self._retire(db_session, job_row, GenerationJobStatus.CANCELLED, now)
                return None

            # 固定输入边界：有效快照决定完整轮读取起点
            working_snapshot = await self._load_valid_snapshot(db_session, turn_row.session_id)
            # 直接读取交接事务保存的固定引用，不再动态查询前驱的摘要或最大轮次。
            inherited_context = SQLiteValueCodec.inherited_context_from_json(session_row.inherited_context_json)
            if session_row.predecessor_session_id is not None and inherited_context is None:
                raise InvalidStateError("后继session缺少固定继承材料")
            # 按升序排列的消息ID列表，包含当前turn的所有消息
            message_ids = (await db_session.scalars(
                select(MessageRow.message_id)
                .where(MessageRow.turn_id == turn_row.turn_id)
                .order_by(MessageRow.message_seq)
            )).all()
            # T2-A固定输入边界
            boundary = GenerationInputBoundary.fix(
                conversation_id=conversation_row.conversation_id,
                character_id=conversation_row.character_id,
                session_id=turn_row.session_id,
                turn_id=turn_row.turn_id,
                turn_seq=turn_row.turn_seq,
                input_version=turn_row.input_version,
                message_ids=message_ids,
                working_snapshot=working_snapshot,
                inherited_context=inherited_context,
            )

            # 在数据库中登记generation_attempt：attempt与turn持有同一个新token，T3据此拒绝迟到结果
            attempt_id = IdGenerator.new("attempt")
            # 一个turn可因新输入创建多个job，序号必须遵守(turn_id, attempt_version)唯一约束。
            last_attempt_seq = await db_session.scalar(select(func.max(GenerationAttemptRow.attempt_version))
                .where(GenerationAttemptRow.turn_id == turn_row.turn_id)) or 0
            db_session.add(reply_mappers.generation_attempt_to_row(GenerationAttempt.start(
                attempt_id=attempt_id,
                job_id=job_id,
                turn_id=turn_row.turn_id,
                attempt_seq=last_attempt_seq + 1,
                input_version=job_row.expected_input_version,
                active_token=turn_row.active_token + 1,
                input_boundary=boundary,
                now=now,
            )))
            # 同步更新到job_row
            job_row.attempt_count += 1
            job_row.current_attempt_id = attempt_id
            job_row.updated_at = now
            # 同步更新到turn_row
            turn_row.active_attempt_id = attempt_id
            turn_row.active_token += 1
            turn_row.row_version += 1
            await db_session.flush()
            # T2-A结束，返回生成开始票据
            return GenerationStartTicket(
                job_id=job_id, attempt_id=attempt_id, turn_id=turn_row.turn_id,
                input_version=turn_row.input_version, active_token=turn_row.active_token,
                input_boundary=boundary,
            )

    async def get_attempt(self, attempt_id: str) -> GenerationAttempt | None:
        """返回提交后的纯业务材料，用于恢复need_generate/need_accept阶段。"""
        async with self._database.db_session() as db:
            row = await db.get(GenerationAttemptRow, attempt_id)
            if row is None:
                return None
            if row.frozen_prompt_json is not None:
                return self._checked_materials(row, ReplyAcceptanceTicket(
                    row.attempt_id, row.input_version, row.active_token, row.prompt_hash, "",
                ))
            return reply_mappers.generation_attempt_to_domain(row)

    @stage("generation.prompt.freeze")
    async def freeze_prompt(self, ticket: GenerationFreezeTicket) -> bool:
        """T2-B：资格、固定输入、summary及审查证据同事务校验和提交。"""
        now = datetime.now(timezone.utc)
        async with self._database.transaction() as db:
            rows = await self._eligible(db, ticket.attempt_id, ticket.input_version, ticket.active_token, now)
            if rows is None:
                return False
            job, attempt, turn, session, conversation = rows
            if (job.job_id != ticket.job_id or turn.turn_id != ticket.turn_id
                    or session.session_id != ticket.session_id):
                return False
            if job.state != GenerationJobStatus.NEED_PREPARE.value or attempt.attempt_state != AttemptState.RETRIEVING.value:
                return False
            domain = reply_mappers.generation_attempt_to_domain(attempt)
            boundary, prompt = domain.input_boundary, ticket.frozen_prompt
            if prompt.character_id and prompt.character_id != boundary.character_id:
                raise InvalidStateError("润色场景角色与固定输入边界不一致")
            manifest = prompt.context_manifest
            if (manifest.current_turn != boundary.current_turn
                    or manifest.complete_turns != boundary.complete_turns
                    or manifest.working_snapshot != boundary.working_snapshot
                    or manifest.inherited_context != boundary.inherited_context):
                raise InvalidStateError("提示词清单与T2-A固定输入边界不一致")
            if not prompt.messages:
                raise InvalidStateError("不能冻结空提示词")
            inputs = (await db.scalars(select(MessageRow).where(
                MessageRow.turn_id == turn.turn_id, MessageRow.role == 'user',
            ).order_by(MessageRow.message_seq))).all()
            expected = tuple(PromptMessage('user', row.content) for row in inputs)
            if not expected or prompt.messages[-len(expected):] != expected:
                raise InvalidStateError("最终提示词没有完整保留本轮用户原文及顺序")
            refs = (*prompt.memory_manifest.daily, *prompt.memory_manifest.topic, *prompt.memory_manifest.other)
            summary_hash = hashlib.sha256(prompt.memory_summary.encode('utf-8')).hexdigest() if prompt.memory_summary else ""
            if prompt.memory_summary_hash != summary_hash:
                raise InvalidStateError("记忆summary hash不一致")
            audit = ticket.memory_audit
            if audit is None:
                if prompt.memory_summary or refs:
                    raise InvalidStateError("记忆summary缺少审查证据")
            elif (audit.attempt_id != attempt.attempt_id or audit.status not in ('ready', 'empty')
                    or audit.summary_hash != summary_hash or set(audit.source_refs) != set(refs)
                    or (audit.status == 'ready' and not prompt.memory_summary.strip())
                    or (audit.status == 'empty' and (prompt.memory_summary or refs or audit.source_cards))):
                raise InvalidStateError("记忆summary、来源与审查证据不一致")
            if prompt.memory_summary and not any(prompt.memory_summary in message.content for message in prompt.messages):
                raise InvalidStateError("最终模型输入缺少记忆summary")
            domain.frozen_prompt, domain.memory_audit = prompt, audit
            mapped = reply_mappers.generation_attempt_to_row(domain, prompt_hash=ReplyFingerprint.prompt_hash(prompt.messages))
            for column in ('frozen_prompt_json', 'context_manifest_json', 'memory_manifest_json', 'memory_audit_json', 'prompt_hash'):
                setattr(attempt, column, getattr(mapped, column))
            attempt.attempt_state = AttemptState.GENERATING.value
            attempt.updated_at = now
            self._advance(job, GenerationJobStatus.NEED_GENERATE, now)
            await db.flush()
            return True

    @stage("generation.candidate.save")
    async def save_candidate(self, ticket: ReplyAcceptanceTicket, candidate: ReplyCandidate) -> bool:
        """锁外生成后保存候选；已提交的候选可以直接恢复到T3，不重复调用模型。"""
        self._validate_candidate(ticket, candidate)
        now = datetime.now(timezone.utc)
        async with self._database.transaction() as db:
            rows = await self._eligible(db, ticket.attempt_id, ticket.input_version, ticket.active_token, now)
            if rows is None:
                return False
            job, attempt, *_ = rows
            if attempt.attempt_state != AttemptState.GENERATING.value:
                return False
            domain = self._checked_materials(attempt, ticket)
            if job.state == GenerationJobStatus.NEED_ACCEPT.value:
                return domain.candidate == candidate
            if job.state != GenerationJobStatus.NEED_GENERATE.value or domain.candidate is not None:
                return False
            domain.candidate = candidate
            attempt.candidate_json = reply_mappers.generation_attempt_to_row(domain, prompt_hash=attempt.prompt_hash).candidate_json
            attempt.updated_at = now
            self._advance(job, GenerationJobStatus.NEED_ACCEPT, now)
            await db.flush()
            return True

    @stage("generation.reply.accept")
    async def accept_reply(
        self, ticket: ReplyAcceptanceTicket, *, turn_token_count: int, tokenizer_version: str,
    ) -> AcceptedReply | None:
        """T3：原子保存正式回复、完成状态及事件；幂等重试不重复登记或累计。"""
        if type(turn_token_count) is not int or turn_token_count < 0 or not tokenizer_version.strip():
            raise ValueError("必须提供锁外统计的非负本轮原文token数及tokenizer版本")
        now = datetime.now(timezone.utc)
        # 开启事务
        async with self._database.transaction() as db:
            attempt = await db.get(GenerationAttemptRow, ticket.attempt_id)
            if attempt is None:
                return None
            # 检查输入版本，版本不对就放弃
            if attempt.input_version != ticket.input_version or attempt.active_token != ticket.active_token:
                return None
            # 已采用的结果始终返回原消息，包括会话后来封存或已开始下一轮的情况。
            if attempt.attempt_state == AttemptState.ACCEPTED.value:
                domain = self._checked_materials(attempt, ticket)
                if domain.candidate is None:
                    raise InvalidStateError("已采用attempt缺少候选")
                self._validate_candidate(ticket, domain.candidate)
                turn = await db.get(TurnRow, attempt.turn_id)
                message = await db.get(MessageRow, turn.accepted_message_id)
                session = await db.get(SessionRow, turn.session_id)
                return self._accepted(session, turn, message)
            # 未采用的回复需要检查
            rows = await self._eligible(db, ticket.attempt_id, ticket.input_version, ticket.active_token, now)
            if rows is None:
                return None
            job, attempt, turn, session, conversation = rows
            if job.state != GenerationJobStatus.NEED_ACCEPT.value or attempt.attempt_state != AttemptState.GENERATING.value:
                return None
            # 输入材料的hash值检查
            domain = self._checked_materials(attempt, ticket)
            candidate = domain.candidate
            if candidate is None:
                raise InvalidStateError("need_accept缺少已持久化候选")
            self._validate_candidate(ticket, candidate)
            # 插入回复消息，并flush
            last_seq = await db.scalar(select(func.max(MessageRow.message_seq)).where(MessageRow.turn_id == turn.turn_id)) or 0
            message = MessageRow(message_id=IdGenerator.new('msg'), turn_id=turn.turn_id,
                message_seq=last_seq + 1, role='character', content=candidate.reply.response_zh,
                visual_json=conversation_mappers.visual_to_json(candidate.reply.visual), created_at=now)
            db.add(message)
            await db.flush()
            # 更新turn的状态
            turn.accepted_message_id, turn.turn_state = message.message_id, TurnState.COMPLETE.value
            turn.row_version += 1
            turn.turn_token_count, turn.tokenizer_version = turn_token_count, tokenizer_version
            # 更新session的进度
            session.session_progress_tokens += turn_token_count
            turn.cumulative_token_count = session.session_progress_tokens
            # 更新conversation的状态
            conversation.updated_at = now
            attempt.attempt_state, attempt.completed_at, attempt.updated_at = AttemptState.ACCEPTED.value, now, now
            # 更新job的状态
            self._advance(job, GenerationJobStatus.DONE, now)
            job.finished_at = now
            # 事件与正式回复一起提交；event_id 由 SQLite 自增分配。
            accepted = self._accepted(session, turn, message)
            db.add(ChatEventRow(
                conversation_id=conversation.conversation_id, job_id=job.job_id,
                event_type="reply_committed", message_id=message.message_id, created_at=now,
                reply_json=conversation_mappers.accepted_reply_to_json(accepted),
            ))
            memory_manifest = domain.frozen_prompt.memory_manifest
            await SQLiteMemoryRetentionRepository.record_memory_use_in_transaction(
                db, character_id=conversation.character_id,
                refs=(*memory_manifest.daily, *memory_manifest.topic),
                used_on=BackendTime.local(now).date(),
            )
            # 完整轮提交时判断：80%准备交接，95%停止向旧窗口开启下一轮。
            await self._advance_session_handoff(
                db, session, turn, now, inherited_context=domain.input_boundary.inherited_context,
            )
            await db.flush()
            return accepted

    async def _eligible(
        self, db: AsyncSession, attempt_id: str, input_version: int, active_token: int, now: datetime,
        *, verify_boundary: bool = True,
    ) -> tuple[GenerationJobRow, GenerationAttemptRow, TurnRow, SessionRow, ConversationRow] | None:
        """写入数据库前需要进行六张表的互查：
        ticket: ReplyAcceptanceTicket本质是从调用回复模型前从数据库读来出来，调用回复模型后组装出来的
        此时数据库有可能有修改，需要检查与
        generation_attempts, generation_jobs,turns, sessions, conversations, messages有没有冲突。
        """
        # 检查跟GenerationAttemptRow是否有冲突
        attempt = await db.get(GenerationAttemptRow, attempt_id)
        if attempt is None or attempt.input_version != input_version or attempt.active_token != active_token:
            return None
        # 检查上层的GenerationJobRow是否有冲突，并且turn有没有冲突，跟job直接相关的就是turn
        job = await db.get(GenerationJobRow, attempt.job_id)
        turn = await db.get(TurnRow, attempt.turn_id)
        if (job is None or turn is None or job.turn_id != turn.turn_id
                or job.current_attempt_id != attempt_id or turn.active_attempt_id != attempt_id
                or job.state not in ('need_prepare', 'need_generate', 'need_accept')
                or attempt.attempt_state not in ('retrieving', 'generating')):
            return None
        status = None
        # 检查turn有没有，是不是CURRENT，输入版本对不对
        session = await db.get(SessionRow, turn.session_id)
        conversation = await db.get(ConversationRow, session.conversation_id) if session else None
        if (turn.turn_state != TurnState.CURRENT.value or turn.accepted_message_id is not None
                or turn.input_version != input_version or job.expected_input_version != input_version
                or turn.active_token != active_token):
            # 输入版本不对，需要废弃OBSOLETE
            status = GenerationJobStatus.OBSOLETE
        elif (session is None or conversation is None or session.session_state != SessionState.OPEN.value
                or conversation.state != ConversationState.ACTIVE.value
                or conversation.current_session_id != session.session_id):
            # 如果会话不存在或者会话已关闭，需要CANCELLED
            status = GenerationJobStatus.CANCELLED
        elif verify_boundary:
            # 上面都没问题就验证输入边界
            boundary = reply_mappers.boundary_from_json(attempt.input_boundary_json)
            ids = tuple((await db.scalars(select(MessageRow.message_id).where(
                MessageRow.turn_id == turn.turn_id, MessageRow.role == 'user').order_by(MessageRow.message_seq))).all())
            if (boundary.session_id != session.session_id or boundary.conversation_id != conversation.conversation_id
                    or boundary.character_id != conversation.character_id
                    or boundary.current_turn.turn_id != turn.turn_id
                    or boundary.current_turn.input_version != input_version
                    or boundary.current_turn.message_ids != ids):
                status = GenerationJobStatus.OBSOLETE
        if status:
            attempt.attempt_state = AttemptState.SUPERSEDED.value
            attempt.completed_at = attempt.updated_at = now
            await self._retire(db, job, status, now)
            return None
        return job, attempt, turn, session, conversation

    @staticmethod
    def _checked_materials(attempt: GenerationAttemptRow, ticket: ReplyAcceptanceTicket) -> GenerationAttempt:
        # 反序列化成GenerationAttempt对象，根据hash值做一次检查，看一下原文和存储时候的hash是否还对得上
        domain = reply_mappers.generation_attempt_to_domain(attempt)
        prompt = domain.frozen_prompt
        # 检查
        if (prompt is None or attempt.prompt_hash != ticket.prompt_hash
                or ReplyFingerprint.prompt_hash(prompt.messages) != attempt.prompt_hash
                or attempt.context_manifest_json != attempt.frozen_prompt_json['context_manifest']
                or attempt.memory_manifest_json != attempt.frozen_prompt_json['memory_manifest']):
            raise InvalidStateError("候选与已冻结提示词不一致或冻结材料损坏")
        boundary, manifest = domain.input_boundary, prompt.context_manifest
        stored_polish_hash = attempt.frozen_prompt_json.get("polish_context_hash")
        if stored_polish_hash is not None and stored_polish_hash != ReplyFingerprint.polish_context_hash(prompt):
            raise InvalidStateError("已冻结润色材料损坏")
        stored_generation_hash = attempt.frozen_prompt_json.get("generation_context_hash")
        if stored_generation_hash is not None and stored_generation_hash != ReplyFingerprint.generation_context_hash(prompt):
            raise InvalidStateError("已冻结场景或锚点材料损坏")
        if (manifest.current_turn != boundary.current_turn or manifest.complete_turns != boundary.complete_turns
                or manifest.working_snapshot != boundary.working_snapshot
                or manifest.inherited_context != boundary.inherited_context):
            raise InvalidStateError("持久化提示词清单与固定输入边界不一致")
        return domain

    @staticmethod
    def _validate_candidate(ticket: ReplyAcceptanceTicket, candidate: ReplyCandidate) -> None:
        if (candidate.prompt_hash != ticket.prompt_hash or candidate.candidate_hash != ticket.candidate_hash
                or ReplyFingerprint.candidate_hash(candidate.reply) != candidate.candidate_hash
                or not candidate.reply.response_zh.strip() or candidate.reply.visual is None):
            raise InvalidStateError("候选正文、视觉状态或hash不一致")
        if candidate.generation_trace is not None:
            trace = dict(candidate.generation_trace)
            digest = trace.pop("trace_hash", None)
            expected = hashlib.sha256(json.dumps(trace, ensure_ascii=False, sort_keys=True).encode()).hexdigest()
            if (digest != expected or trace.get("original_prompt_hash") != ticket.prompt_hash
                    or trace.get("final_reply_hash") != candidate.candidate_hash):
                raise InvalidStateError("生成证据与候选不一致")
            if (trace.get("schema") == "two-stage-scene-reply-v2"
                    and trace.get("draft_visual") != conversation_mappers.visual_to_json(candidate.reply.visual)):
                raise InvalidStateError("最终动作与第一阶段动作不一致")
            if (trace.get("schema") == "single-stage-scene-reply-v1"
                    and (trace.get("reply_visual") != conversation_mappers.visual_to_json(candidate.reply.visual)
                         or trace.get("reply") != candidate.reply.response_zh)):
                raise InvalidStateError("单阶段生成证据与最终回复不一致")

    @staticmethod
    def _advance(job: GenerationJobRow, state: GenerationJobStatus, now: datetime) -> None:
        job.state, job.updated_at = state.value, now
        job.retry_count, job.failure_json = 0, None
        job.available_at = now

    @staticmethod
    def _accepted(session: SessionRow, turn: TurnRow, message: MessageRow | None) -> AcceptedReply:
        if message is None or message.turn_id != turn.turn_id or message.role != 'character':
            raise InvalidStateError("正式回复记录缺失或所属轮次错误")
        return AcceptedReply(session.conversation_id, turn.turn_id, message.message_id,
                             message.content, conversation_mappers.visual_from_json(message.visual_json), message.created_at)

    async def _advance_session_handoff(
        self, db: AsyncSession, session: SessionRow, turn: TurnRow, now: datetime,
        *, inherited_context: InheritedContextRef | None,
    ) -> None:
        """完整轮后登记一次准备任务；达到95%标记等待交接，不等待模型或切换session。"""
        policy = self._lifecycle_policy
        # 使用本次生成固定的继承引用，不能查询前驱最新摘要来冒充入场版本。
        inherited_tokens = await self._read_inherited_token_count(db, inherited_context)
        decision = policy.decide_context_maintenance(
            session_id=session.session_id, through_turn_seq=turn.turn_seq,
            session_progress_tokens=session.session_progress_tokens,
            inherited_tokens=inherited_tokens, handoff_ready=False,
            at_complete_turn_boundary=True,
        )
        if LifecycleAction.PREPARE_HANDOFF not in decision.actions:
            return
        # 同一session重复触发时沿用原任务的首次冻结上界（不追最新消息）。
        await self._context_repository.create_job_in_transaction(db, CreateContextJobCommand(
            job_type=ContextJobType.PREPARE_HANDOFF,
            session_id=session.session_id,
            frozen_through_turn_seq=turn.turn_seq,
            priority=10,
            dedupe_key=f'prepare_handoff:{session.session_id}',
        ))
        if LifecycleAction.WAIT_FOR_CONTEXT in decision.actions:
            session.session_state = SessionState.WAITING_HANDOFF.value
            await db.flush()
            await self._context_repository.commit_handoff_in_transaction(db, session_id=session.session_id)

    async def _read_inherited_token_count(
        self, db: AsyncSession, ref: InheritedContextRef | None,
    ) -> int:
        """按固定引用累计继承摘要与连续尾部，不预留未使用的摘要额度。"""
        if ref is None:
            return 0
        total = 0
        if ref.summary_snapshot_ref is not None:
            row = await db.get(ContextSnapshotRow, ref.summary_snapshot_ref.snapshot_id)
            if (row is None or row.session_id != ref.predecessor_session_id
                    or context_mappers.snapshot_to_ref(row) != ref.summary_snapshot_ref
                    or row.snapshot_state != SnapShotState.VALID.value):
                raise InvalidStateError("固定继承摘要缺失、未采用或引用不匹配")
            total += row.rendered_token_count
        scope = ref.source_range
        if scope is not None and scope.from_seq <= scope.through_seq:
            count, tokens = (await db.execute(select(
                func.count(TurnRow.turn_id), func.coalesce(func.sum(TurnRow.turn_token_count), 0),
            ).where(
                TurnRow.session_id == scope.session_id,
                TurnRow.turn_seq.between(scope.from_seq, scope.through_seq),
                TurnRow.turn_state == TurnState.COMPLETE.value,
            ))).one()
            if count != scope.through_seq - scope.from_seq + 1:
                raise InvalidStateError("继承原文尾部缺失或尚未完成")
            total += tokens
        return total

    async def _load_valid_snapshot(
        self, db_session: AsyncSession, session_id: str,
    ) -> SnapshotRef | None:
        """读取session唯一已采用摘要的引用；session_id唯一约束保证最多一份

        输入:
        session_id
        输出:
        SnapshotRef: {snapshot_id, content_hash, covered_through_turn_seq}|None
        """
        snapshot_row = await db_session.scalar(
            select(ContextSnapshotRow).where(
                ContextSnapshotRow.session_id == session_id,
                ContextSnapshotRow.snapshot_state == SnapShotState.VALID.value,
            )
        )
        return context_mappers.snapshot_to_ref(snapshot_row) if snapshot_row is not None else None

    async def _retire(
        self, db_session: AsyncSession, job_row: GenerationJobRow,
        status: GenerationJobStatus, now: datetime,
    ) -> None:
        """任务进入终态并落finished_at；正常出事务上下文即提交

        输入:
        job_row: GenerationJobRow, status, now
        输出:
        None（原地修改job_row的state、updated_at、finished_at）
        """
        job_row.state = status.value
        job_row.updated_at = now
        job_row.finished_at = now
        await db_session.flush()
