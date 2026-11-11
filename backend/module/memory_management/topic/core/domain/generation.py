"""Topic 批量生成结果、主题更新申请及模型输出校验。

定义新建和更新动作、条目操作、单篇申请与批次结果，校验本批 object 的采用和暂存处置。
TopicModelOutput 解析模型变更协议，结合固定来源回填证据并合并完整候选；审核和正式发布产物由其他文件定义。
"""
from __future__ import annotations
import hashlib
import json
from module.shared.common.errors.base import InvalidStateError
from module.shared.memory.types.references import EvidenceRef, MemoryKind, MemoryRevisionRef
from dataclasses import asdict, dataclass, replace
from datetime import date, datetime, timezone
from enum import StrEnum
from module.memory_management.object.core.domain.document import MemoryObject
from module.memory_management.topic.core.domain.document import (
    TopicCurrentState,
    TopicDocument,
    TopicEntry,
    TopicIdentity,
)
from module.memory_management.topic.core.domain.plan import TopicPlan
from module.memory_management.topic.core.domain.source import TopicSourceInput
from uuid import NAMESPACE_URL, uuid5


class TopicAction(StrEnum):
    CREATE = "create"
    UPDATE = "update"


class TopicOperationKind(StrEnum):
    MERGE_EVIDENCE = "merge_evidence"
    REVISE_EVENT = "revise_event"
    ADD_EVENT = "add_event"
    CORRECT_EVENT = "correct_event"


@dataclass(frozen=True, slots=True)
class TopicOperation:
    kind: TopicOperationKind
    object_ids: tuple[str, ...]
    evidence: tuple[EvidenceRef, ...]
    target_entry_id: str | None = None
    proposed_entry: TopicEntry | None = None


@dataclass(frozen=True, slots=True)
class TopicApplication:
    """一篇topic的新建或更新申请；topic_id由程序分配，模型不决定存储身份。"""
    application_id: str
    topic_id: str
    action: TopicAction
    object_ids: tuple[str, ...]
    document: TopicDocument
    base_ref: MemoryRevisionRef | None = None
    discussion_session_ids: tuple[str, ...] = ()  # 仍须结合原文核实实质讨论
    operations: tuple[TopicOperation, ...] = () #每个topic对应的申请操作
    discussion_source_refs: tuple[EvidenceRef, ...] = ()
    reason: str = ""

    def __post_init__(self) -> None:
        if not self.application_id or not self.topic_id or not self.object_ids:
            raise ValueError("topic申请需要稳定ID及采用的object")
        if len(set(self.object_ids)) != len(self.object_ids):
            raise ValueError("topic申请中的object不能重复")
        if self.action == TopicAction.CREATE:
            if self.base_ref is not None:
                raise ValueError("新建topic不能绑定基础版本")
        elif self.action == TopicAction.UPDATE:
            if (self.base_ref is None or self.base_ref.kind != MemoryKind.TOPIC
                    or self.base_ref.memory_id != self.topic_id):
                raise ValueError("更新topic必须绑定对应的基础版本")
        else:
            raise ValueError("未知topic申请类型")
        if len(set(self.discussion_session_ids)) != len(self.discussion_session_ids):
            raise ValueError("实质讨论的session不能重复计数")
        if any(not set(operation.object_ids) <= set(self.object_ids) for operation in self.operations):
            raise ValueError("topic操作引用了申请之外的object")


@dataclass(frozen=True, slots=True)
class TopicBatchResult:
    """一次生成可产生多篇申请；未采用的本批object明确继续暂存。"""
    result_version: int
    applications: tuple[TopicApplication, ...]
    pending_object_ids: tuple[str, ...]
    content_hash: str
    prompt_hash: str
    generated_at: datetime

    def __post_init__(self) -> None:
        if type(self.result_version) is not int or self.result_version < 1:
            raise ValueError("topic候选版本必须为正整数")
        if len({item.application_id for item in self.applications}) != len(self.applications):
            raise ValueError("topic申请ID不能重复")
        if len({item.topic_id for item in self.applications}) != len(self.applications):
            raise ValueError("同一批次每个topic只能有一份完整申请")
        used = [cid for item in self.applications for cid in item.object_ids]
        if len(set(used)) != len(used):
            raise ValueError("同一object不能在本批归入多个topic")
        if (len(set(self.pending_object_ids)) != len(self.pending_object_ids)
                or set(used) & set(self.pending_object_ids)):
            raise ValueError("暂存object不能重复或同时被申请采用")

    def validate_against_plan(self, plan: TopicPlan) -> None:
        """对照本批计划校验候选结果的引用范围、处置覆盖与新建门槛。"""
        new = set(plan.object_ids)
        available = new | set(plan.related_object_ids)
        used = {cid for app in self.applications for cid in app.object_ids}
        pending_ids = set(self.pending_object_ids)
        if not used <= available or not pending_ids <= new or (used & new) | pending_ids != new:
            raise ValueError("topic生成结果必须完整处置本批object，且不得引用计划外object")
        for app in self.applications:
            if not set(app.object_ids) & new:
                raise ValueError("topic申请必须涉及本批object")
            if app.base_ref is not None and app.base_ref not in plan.topic_refs:
                raise ValueError("topic申请使用了未读取的基础版本")
            if app.action == TopicAction.CREATE and len(app.discussion_session_ids) < plan.min_distinct_sessions:
                raise ValueError("新建topic的不同session数不足；来源真实性另由原文核对")


@dataclass(frozen=True, slots=True)
class TopicModelOutput:
    """模型边界：JSON解析、引用回填和候选构造归在同一个输出类内。"""
    result: TopicBatchResult

    @classmethod
    def from_model_data(cls, data: dict, *, source: TopicSourceInput, prompt: str) -> "TopicModelOutput":
        """回填输入中的引用与基础版本，再构造可保存、可审核的完整候选。"""
        try:
            if not isinstance(data, dict) or set(data) != {"applications", "pending_object_ids"}:
                raise ValueError("topic输出顶层字段不符合约定")
            objects = {item.object_id: item for group in source.groups
                       for item in (group.object, *group.related_objects)}
            topics = {item.ref.memory_id: item for group in source.groups for item in group.existing_topics}
            applications = tuple(cls._application(row, source, objects, topics) for row in data["applications"])
            pending_object_ids = tuple(data["pending_object_ids"])
            content = {"applications": [asdict(item) for item in applications], "pending_object_ids": pending_object_ids}
            encoded = json.dumps(content, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)
            result = TopicBatchResult(1, applications, pending_object_ids, hashlib.sha256(encoded.encode("utf-8")).hexdigest(),
                                       hashlib.sha256(prompt.encode("utf-8")).hexdigest(), datetime.now(timezone.utc))
            result.validate_against_plan(source.plan)
            return cls(result)
        except (KeyError, TypeError, ValueError) as error:
            raise InvalidStateError(f"topic模型输出不符合本批输入：{error}") from error

    @staticmethod
    def _object_refs(objects: tuple[MemoryObject, ...], *, only_user: bool = False) -> dict:
        # 收集这些object里出现过的全部消息出处（哪条消息在哪个session）；
        # only_user=True 时只收集用户自己说的条目。
        result = {}
        for item in objects:
            for entry in (*item.events, *item.plans):
                if only_user and entry.actor != "user":
                    continue
                for ref in entry.source_refs:
                    result[(ref.session_id, ref.message_id)] = ref
        return result

    @staticmethod
    def _refs(rows: list[dict], allowed_document_refs: dict, *, required: bool = True) -> tuple[EvidenceRef, ...]:
        if not isinstance(rows, list) or required and not rows:
            raise ValueError("topic来源引用必须为数组，事实必须有来源")
        result = []
        for row in rows:
            if set(row) != {"session_id", "message_id"}:
                raise ValueError("topic来源只接受session_id和message_id")
            key = (row["session_id"], row["message_id"])
            if key not in allowed_document_refs:
                raise ValueError("topic引用了本申请输入之外的消息")
            result.append(allowed_document_refs[key])
        return tuple(dict.fromkeys(result))

    @classmethod
    def _document(cls, data: dict, *, allowed_document_refs: dict,
                  selected_object_refs: dict, base_document: TopicDocument | None,
                  visible_entry_ids: set[str]) -> TopicDocument:
        """解析模型提交的变更，在当前基础版本上合并为完整正文。"""
        entries = tuple(TopicEntry(
            entry_id=row["entry_id"], statement=row["statement"],
            occurrence_time=date.fromisoformat(row["occurrence_time"]) if row["occurrence_time"] is not None else None,
            acquire_time=date.fromisoformat(row["acquire_time"]),
            source_refs=cls._refs(row["source_refs"], allowed_document_refs, required=False),
            corrects_entry_id=row["corrects_entry_id"],
            kind=row["kind"], actor=row["actor"],
            target_time=date.fromisoformat(row["target_time"]) if row["target_time"] is not None else None,
        ) for row in data["entries"])
        if len({entry.entry_id for entry in entries}) != len(entries):
            raise ValueError("topic变更中的条目ID不能重复")
        old_entries = {entry.entry_id: entry for entry in base_document.entries} if base_document is not None else {}
        for entry in entries:
            old = old_entries.get(entry.entry_id)
            if old is not None and entry.entry_id not in visible_entry_ids:
                raise ValueError("只能修改本次展示了正文的旧条目")
            if entry.corrects_entry_id is not None and entry.corrects_entry_id not in visible_entry_ids:
                raise ValueError("更正只能指向本次展示了正文的旧条目")
            if old is None:
                if not entry.source_refs or any((ref.session_id, ref.message_id) not in selected_object_refs
                                                for ref in entry.source_refs):
                    raise ValueError("新增时间线条目必须引用本申请采用的object，不能借旧topic凭空补进展")
            elif replace(entry, source_refs=old.source_refs) == old:
                # 比较全部正文属性；只沿用旧ID不足以获得空来源豁免。
                if old.source_refs and not entry.source_refs:
                    raise ValueError("保留旧条目不能清空已有来源")
            elif not any((ref.session_id, ref.message_id) in selected_object_refs for ref in entry.source_refs):
                raise ValueError("修改旧条目必须引用本申请采用的object")
        old_identity = base_document.topic_identity if base_document is not None else None
        identity_data = data["topic_identity"]
        if identity_data is None:
            if old_identity is None:
                raise ValueError("新建topic必须提供主题说明")
            identity = old_identity
        else:
            identity = TopicIdentity(identity_data["description"],
                cls._refs(identity_data["source_refs"], allowed_document_refs, required=False))
        if old_identity is not None and identity.description == old_identity.description:
            if old_identity.source_refs and not identity.source_refs:
                raise ValueError("保留主题说明不能清空已有来源")
        elif not any((ref.session_id, ref.message_id) in selected_object_refs for ref in identity.source_refs):
            raise ValueError("新增或修改主题说明必须引用本申请采用的object")
        state_data = data["current_state"]
        if state_data is None:
            if base_document is None:
                raise ValueError("新建topic必须提供当前状态")
            state = base_document.current_state
        else:
            state = TopicCurrentState(state_data["text"], tuple(state_data["source_entry_ids"]))
            if not set(state.source_entry_ids) <= visible_entry_ids | {entry.entry_id for entry in entries}:
                raise ValueError("当前状态只能引用本次展示的旧条目或本次新增条目")
        merged = dict(old_entries)
        merged.update((entry.entry_id, entry) for entry in entries)
        order = {entry_id: index for index, entry_id in enumerate(merged)}
        return TopicDocument(data["topic"], identity, state, tuple(sorted(merged.values(),
            key=lambda entry: (entry.acquire_time, order[entry.entry_id]))))

    @classmethod
    def _application(cls, data: dict, source: TopicSourceInput, objects: dict, topics: dict) -> TopicApplication:
        """解析并校验一份申请。
        模型收到一批object和已有topic，交回一份变更方案（JSON），
        声明"我要新建/更新某个topic，采用哪些object，做哪些改动"。
        这个方法逐项核对这份方案——它说的每个消息出处是否真实存在、
        它声明的改动是否合规——核对通过才转成可以入库的对象。任何一条不符就整体拒绝。
        """
        if "operations" not in data:
            raise ValueError("topic申请缺少operations：operations必须与document同级，不能放在document内")
        # 确认申请里说的object真实存在：模型说"我采用了这几个object"，
        # 这里核对输入里确实有，没有就是模型瞎编，直接拒绝。
        object_ids = tuple(data["object_ids"])
        if not object_ids or any(cid not in objects for cid in object_ids):
            raise ValueError("topic申请采用了输入之外的object")
        selected = tuple(objects[cid] for cid in object_ids)
        # 收集消息出处：采用的那几个object，每条记录都带着出处（来自哪个session哪条消息）。
        # selected_object_refs 把这些出处汇总成一张清单
        #  模型写的每个出处都要能在这张清单里查到，查不到 = 编造来源，拒绝；
        #  新写的内容只能用这张清单里的出处，不能拿别的旧内容当证据。
        selected_object_refs = cls._object_refs(selected)
        action = TopicAction(data["action"])
        target_id = data["target_topic_id"]
        base = None
        base_document = None
        visible_entry_ids: set[str] = set()
        allowed_document_refs = dict(selected_object_refs)
        if action == TopicAction.UPDATE:
            # 修订可沿用本次可见正文的出处；未提交的内容由基础版本直接保留。
            if target_id not in topics:
                raise ValueError("更新申请必须指向已提供的topic")
            base = topics[target_id]
            timeline = base.document.timeline_for(source.timeline_window_date)
            base_document, visible = timeline.document, timeline.visible_entries
            visible_entry_ids = {entry.entry_id for entry in visible}
            allowed_document_refs |= {(ref.session_id, ref.message_id): ref
                        for item in (base.document.topic_identity, *visible)
                        for ref in item.source_refs}
        elif target_id is not None:
            raise ValueError("新建申请不能指定topic存储ID")
        if base is not None:
            old_ids = {entry.entry_id for entry in base.document.entries}
            if any(row["entry_id"] in old_ids - visible_entry_ids for row in data["document"]["entries"]):
                raise ValueError("只能修改本次展示了正文的旧条目")
            if data["document"]["topic"] != base_document.topic:
                raise ValueError("更新不能修改已有topic的名称")
        document = cls._document(
            data["document"], allowed_document_refs=allowed_document_refs,
            selected_object_refs=selected_object_refs,
            base_document=base_document, visible_entry_ids=visible_entry_ids,
        )
        entries = {event.entry_id: event for event in document.entries}
        old_entry_ids = {entry.entry_id for entry in base.document.entries} if base is not None else set()
        submitted_ids = {row["entry_id"] for row in data["document"]["entries"]}
        # 逐条核对模型声明的四种改动（新增 / 补出处 / 补充细节 / 更正），各有各的规则；
        # 最后 topic 和申请的编号由程序生成，模型永远不碰数据库的 ID。
        operations = []
        for row in data["operations"]:
            kind = TopicOperationKind(row["kind"])
            operation_object_ids = tuple(row["object_ids"])
            if not operation_object_ids or not set(operation_object_ids) <= set(object_ids):
                raise ValueError("topic操作引用了本申请未采用的object")
            target_entry_id, proposed_id = row["target_entry_id"], row["proposed_entry_id"]
            if proposed_id is not None and proposed_id not in submitted_ids:
                raise ValueError("操作的proposed_entry_id必须指向本次提交的条目")
            proposed = entries[proposed_id] if proposed_id is not None else None
            if kind == TopicOperationKind.ADD_EVENT:
                if target_entry_id is not None or proposed is None or proposed_id in old_entry_ids:
                    raise ValueError("新增进展必须指向新增条目且不指定旧目标")
            elif target_entry_id not in visible_entry_ids:
                raise ValueError("补证、补充、更正只能指向本次展示的旧条目")
            if kind == TopicOperationKind.MERGE_EVIDENCE and proposed_id is not None:
                raise ValueError("补证操作不提交替代条目")
            if kind == TopicOperationKind.REVISE_EVENT and (proposed is None or proposed_id != target_entry_id):
                raise ValueError("补充操作必须保持旧条目ID")
            if kind == TopicOperationKind.CORRECT_EVENT and (proposed is None or proposed_id in old_entry_ids
                    or proposed.corrects_entry_id != target_entry_id):
                raise ValueError("更正必须新增条目并指向被更正的旧条目")
            evidence = cls._refs(row["source_refs"], cls._object_refs(tuple(objects[cid] for cid in operation_object_ids)))
            operations.append(TopicOperation(kind, operation_object_ids, evidence, target_entry_id, proposed))
            if kind == TopicOperationKind.MERGE_EVIDENCE:
                target = entries[target_entry_id]
                old = next(entry for entry in base_document.entries if entry.entry_id == target_entry_id)
                if replace(target, source_refs=old.source_refs) != old:
                    raise ValueError("补证操作不能修改条目正文")
                entries[target_entry_id] = replace(target, source_refs=tuple(dict.fromkeys((*target.source_refs, *evidence))))
        old_entries = {entry.entry_id: entry for entry in base_document.entries} if base_document is not None else {}
        changed_ids = {entry_id for entry_id in submitted_ids if entries[entry_id] != old_entries.get(entry_id)}
        covered_ids = {operation.proposed_entry.entry_id for operation in operations if operation.proposed_entry is not None}
        covered_ids |= {operation.target_entry_id for operation in operations if operation.kind == TopicOperationKind.MERGE_EVIDENCE}
        if changed_ids - covered_ids:
            raise ValueError("新增或修改条目必须声明对应操作")
        document = replace(document, entries=tuple(entries.values()))
        discussion_refs = cls._refs(data["discussion_source_refs"], cls._object_refs(selected, only_user=True), required=False)
        session_ids = tuple(dict.fromkeys(ref.session_id for ref in discussion_refs))
        # 稳定身份由程序生成，不能让模型填写数据库ID；采用计划中的固定顺序。
        ordered_ids = tuple(cid for cid in (*source.plan.object_ids, *source.plan.related_object_ids) if cid in object_ids)
        topic_id = target_id if base is not None else str(uuid5(NAMESPACE_URL,
            json.dumps(["topic", source.plan.character_id, ordered_ids], ensure_ascii=False)))
        application_id = str(uuid5(NAMESPACE_URL, json.dumps([action.value, topic_id, ordered_ids], ensure_ascii=False)))
        return TopicApplication(application_id, topic_id, action, object_ids, document,
            base.ref if base is not None else None, session_ids, tuple(operations), discussion_refs, data["reason"])
