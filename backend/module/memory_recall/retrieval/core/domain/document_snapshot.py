"""单轮召回的正式文档快照及时间规则。"""
from __future__ import annotations

import hashlib
import json
from datetime import date, datetime, timedelta
from module.shared.common.time import BackendTime

from module.memory_recall.retrieval.core.domain.document import RecallDocument
from module.memory_recall.retrieval.core.domain.tools.common import TimeContext, TimeRange, TopicTimeContext


class RecallDocumentSnapshot:
    def __init__(self, documents: tuple[RecallDocument, ...]) -> None:
        self.payloads = tuple(payload for item in documents if (payload := item.to_recall_payload())["sections"])

    @staticmethod
    def _stamp(value: str) -> float:
        parsed = datetime.fromisoformat(value)
        if parsed.tzinfo is None:
            raise ValueError("Temporal metadata must carry a timezone")
        return parsed.timestamp()


    @staticmethod
    def _day_interval(value: str) -> tuple[datetime, datetime]:
        day = date.fromisoformat(value)
        start = BackendTime.local(day)
        end = BackendTime.local(day + timedelta(days=1))
        return BackendTime.utc(start), BackendTime.utc(end)


    @staticmethod
    def time_context(payload: dict) -> TimeContext | TopicTimeContext:
        common = dict(conversation_time=payload.get("conversation_time"),
            memory_date=payload.get("memory_date"), record_dates=payload.get("record_dates", []),
            time_precision="date")
        if payload["kind"] == "daily":
            return TimeContext(**common)
        intervals, unknown, plans = [], 0, []
        for event in payload.get("temporal_events", []):
            if event.get("superseded"):
                continue
            if event["kind"] == "plan":
                plans.append(dict(entry_id=event["entry_id"], target_time=event.get("target_time"),
                                  acquire_time=event["acquire_time"]))
                continue
            value = event.get("occurrence_time")
            if value is None:
                unknown += 1
                continue
            start, end = RecallDocumentSnapshot._day_interval(value)
            intervals.append(dict(start=start.isoformat(), end=end.isoformat(), precision="date",
                event_status="occurred", source_entry_id=event["entry_id"], acquire_time=event["acquire_time"]))
        return TopicTimeContext(**common, event_time_status=("partial" if unknown else "known") if intervals else "unknown",
            event_intervals=intervals, unknown_event_count=unknown, plan_dates=plans)


    def coverage(self) -> dict:
        payloads = self.payloads
        dates = [value for p in payloads for value in p.get("record_dates", [])]
        digest = [(p["document_id"], p["revision"], p["content_hash"], p.get("record_dates", []), p["pool"]) for p in payloads]
        return dict(scope="authorized_hot_and_cold", metadata_scan_complete=True, includes_hot_and_cold=True,
            document_count=len(payloads), latest_conversation_time=None, earliest_conversation_time=None,
            latest_record_date=max(dates) if dates else None, earliest_record_date=min(dates) if dates else None,
            time_precision="date", source_ingested_through=None,
            snapshot_id=hashlib.sha256(json.dumps(sorted(digest), ensure_ascii=False).encode()).hexdigest())


    def filter_by_time(self, window: TimeRange | None, unknown_event_time: str,
                       *, pool: str | None, kinds: tuple[str, ...]) -> tuple[list[dict], dict]:
        candidates, unknown_docs = [], 0
        for payload in self.payloads:
            if payload["kind"] not in kinds or (pool is not None and payload.get("pool") != pool):
                continue
            if window is None:
                candidates.append(payload)
            elif window.basis == "conversation_time":
                # 兼容工具参数名；正式数据只有日期，按记忆日期/得知日期整日区间与请求求交。
                if any(RecallDocumentSnapshot._day_interval(value)[0] < window.end and RecallDocumentSnapshot._day_interval(value)[1] > window.start
                       for value in payload.get("record_dates", [])):
                    candidates.append(payload)
            elif payload["kind"] == "topic":
                ctx = RecallDocumentSnapshot.time_context(payload)
                unknown = ctx.unknown_event_count > 0
                unknown_docs += int(unknown)
                if any(RecallDocumentSnapshot._stamp(item["start"]) < window.end.timestamp() and RecallDocumentSnapshot._stamp(item["end"]) > window.start.timestamp()
                       for item in ctx.event_intervals) or (unknown and unknown_event_time == "include"):
                    candidates.append(payload)
        return candidates, dict(time_scan_complete=True, time_precision="date",
            time_range_applied=window.model_dump(mode="json") if window else None,
            documents_in_time_range=len(candidates), latest_conversation_time_in_scope=None,
            unknown_event_time_documents=unknown_docs, count_before_text_matching=True,
            unknown_event_time_policy=unknown_event_time if window and window.basis == "event_time" else None)
