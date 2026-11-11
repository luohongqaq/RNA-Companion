"""总结模型输入的确定性投影，不执行模型调用或文件读取。"""
from __future__ import annotations
import copy


_AUDIT_FIELDS = frozenset({
    'source_refs', 'source_entry_ids', 'source_turn_ids', 'source_ids', 'document_id',
    'read_handle', 'content_hash', 'source_hash', 'entry_id', 'source_entry_id',
})


_FIELD_LABELS = {
    'memorable_moments': '值得记住的时刻', 'profile_facts': '长期事实',
    'intentions': '想做与打算', 'commitments': '明确承诺', 'reflections': '我的反思',
    'memory_date': '日记日期', 'record_dates': '记忆日期或得知日期', 'time_precision': '时间精度',
    'plan_dates': '计划时间', 'target_time': '目标日期', 'acquire_time': '得知日期',
    'topic_identity': '主题说明',
    'current_state': '当前状态', 'entries': '事件', 'conversation_time': '来源交流时间',
    'event_time_status': '事件时间完整性', 'event_intervals': '事件时间',
    'unknown_event_count': '发生时间未知的事件数',
    'event_time': '发生时间', 'heard_at': '听说时间',
    'date': '原文时间', 'start': '开始', 'end': '结束',
    'precision': '时间精度', 'event_status': '事件状态',
    'source_event_index': '原事件索引', 'subject': '主体',
    'text': '内容', 'description': '说明', 'summary': '摘要',
}


class MemorySummaryInput:
    @staticmethod
    def _has_content(value):
        return value is not None and not (isinstance(value, str) and not value.strip()) and value != [] and value != {}

    @staticmethod
    def _compact_content(value):
        """保留原文与时间结构；列表只去除完全相同的项目，不拆句或跨卡合并。"""
        if isinstance(value, dict):
            result = {}
            for key, item in value.items():
                if key not in _AUDIT_FIELDS:
                    cleaned = MemorySummaryInput._compact_content(item)
                    if MemorySummaryInput._has_content(cleaned):
                        result[key] = cleaned
            return result
        if isinstance(value, list):
            result = []
            for item in value:
                cleaned = MemorySummaryInput._compact_content(item)
                if MemorySummaryInput._has_content(cleaned) and cleaned not in result:
                    result.append(cleaned)
            return result
        return copy.deepcopy(value)

    @staticmethod
    def _content_lines(label: str, value, level: int = 3) -> list[str]:
        """只转换表示形式；未知字段仍保留名称，不借机概括或推断正文。"""
        if not MemorySummaryInput._has_content(value):
            return []
        if isinstance(value, dict):
            # 单一正文包装直接展开，省去“当前状态→内容”这样的空层级。
            if len(value) == 1 and next(iter(value)) in ('text', 'description', 'summary'):
                return MemorySummaryInput._content_lines(label, next(iter(value.values())), level)
            lines = []
            # 先写同级标量，再展开子标题，避免后续同级字段看似归属上一子项。
            for key, item in sorted(value.items(), key=lambda pair: isinstance(pair[1], (dict, list))):
                lines.extend(MemorySummaryInput._content_lines(_FIELD_LABELS.get(key, key), item,
                                            min(level + 1, 6) if label else level))
            return [f"{'#' * level} {label}", *lines] if lines and label else lines
        if isinstance(value, list):
            lines = []
            for index, item in enumerate(value, 1):
                lines.extend(MemorySummaryInput._content_lines(f'{label or "条目"}{index}' if isinstance(item, (dict, list)) else label,
                                            item, level))
            return lines
        if isinstance(value, bool):
            value = 'true' if value else 'false'
        return [f'{label}：{value}' if label else str(value)]

    @staticmethod
    def _memory_text(memory: dict) -> str:
        kind = {'daily': '日记', 'topic': '主题'}.get(memory['kind'], memory['kind'])
        lines = [f"# {memory['title']}（{kind}）"]
        context = memory['time_context']
        recorded = memory['conversation_time'] or context.get('conversation_time')
        lines.extend(MemorySummaryInput._content_lines('记录时间', recorded))
        time_lines = []
        for key, value in sorted(context.items(), key=lambda pair: isinstance(pair[1], (dict, list))):
            if key == 'conversation_time' and value == recorded:
                continue
            time_lines.extend(MemorySummaryInput._content_lines(_FIELD_LABELS.get(key, key), value))
        if time_lines:
            lines.extend(['', '## 时间信息', *time_lines])
        for key, value in memory['sections'].items():
            content = MemorySummaryInput._content_lines('', value)
            if content:
                lines.extend(['', f'## {_FIELD_LABELS.get(key, key)}', *content])
        return '\n'.join(lines)

    @staticmethod
    def _dialogue_only(chat_context: list) -> list:
        """保留全部对话轮次及时间；不猜测哪些早先前情可以丢弃。"""
        turns = []
        for turn in chat_context:
            if not isinstance(turn, dict):
                turns.append(copy.deepcopy(turn))
            elif turn.get('role') in ('user', 'assistant', 'context') and turn.get('content') is not None:
                turns.append({key: copy.deepcopy(turn[key]) for key in (
                    'role', 'content', 'kind', 'name', 'timestamp', 'time', 'conversation_time',
                    'event_time', 'created_at') if key in turn})
        return turns

    @staticmethod
    def build(selection: dict, user_input: str, chat_context: list,
                            character_context: dict | None = None) -> dict:
        """只传正文及时间，不传来源编号、散列或旧推理；原始卡片另留审计。"""
        memories = []
        for card in selection.get('selected', []):
            source = card.get('source', {})
            memory = {
                'kind': card['kind'], 'title': card['title'],
                'conversation_time': source.get('conversation_time'),
                'time_context': MemorySummaryInput._compact_content(card.get('time_context') or source.get('time_context', {})),
                'sections': MemorySummaryInput._compact_content(card['sections']),
            }
            # 时间和标题也须一致；不同日期的相同措辞仍可能描述不同阶段。
            if memory not in memories:
                memories.append(memory)
        payload = {'TASK_REQUIREMENT': '请在remaining_seconds给出的实际剩余时间内完成记忆总结，直接输出可用记忆。',
                'CURRENT_INPUT': user_input, 'CHAT_CONTEXT': MemorySummaryInput._dialogue_only(chat_context),
                'SELECTION_UNRESOLVED': copy.deepcopy(selection.get('unresolved', [])),
                'MEMORIES': '\n\n'.join(MemorySummaryInput._memory_text(memory) for memory in memories)}
        if character_context is not None:
            payload['CHARACTER_CONTEXT'] = {key: copy.deepcopy(character_context[key])
                                            for key in ('character_name', 'rules')
                                            if key in character_context}
        return payload
