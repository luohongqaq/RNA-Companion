# Memory Recall 类与方法清单

端到端流程见 [ARCHITECTURE.md](ARCHITECTURE.md)，数据边界见 [DOMAIN.md](DOMAIN.md)。
下表列出模块自己声明的类和方法，包括私有方法。只持有数据的类列出字段。

## Domain 领域类

### agent

| 类 | 声明的方法或字段 |
| --- | --- |
| [MemoryAudit](agent/core/domain/audit.py) | `from_context()` |
| [MemorySourceRef](agent/core/domain/audit.py) | `ref_id`, `turn_id`, `ref`, `selected_at` |
| [AttemptMemoryRef](agent/core/domain/audit.py) | `attempt_id`, `ref` |
| [MemoryRecallTaskError](agent/core/domain/errors.py) | — |
| [MemoryModelRequest](agent/core/domain/model.py) | `model`, `messages`, `timeout_seconds`, `tools`, `thinking_enabled`, `reasoning_effort`, `max_output_tokens` |
| [MemoryModelResult](agent/core/domain/model.py) | `message`, `finish_reason`, `request_id`, `usage` |
| [Budget](agent/core/domain/policy.py) | `max_tool_calls`, `output_token_limit`, `final_doc_limit`, `final_token_limit`, `error_reserve` |
| [MemoryModelPort](agent/core/domain/ports.py) | `async complete()` |
| [MemoryRecallTask](agent/core/domain/request.py) | `attempt_id`, `character_id`, `context`, `character_card`, `is_current`, `conversation_id` |
| [SelectionSkill](agent/core/domain/resources.py) | `name`, `description`, `instructions`, `file_hash` |
| [MemoryRecallResources](agent/core/domain/resources.py) | `selection_instructions`, `skill_hash`, `summary_prompt`, `summary_prompt_hash` |
| [MemoryContext](agent/core/domain/result.py) | `prompt_text()` |
| [MemoryExcerpt](agent/core/domain/result.py) | `ref`, `section_key`, `text` |
| [MemorySelection](agent/core/domain/result.py) | `selected_refs`, `excerpts`, `search_again` |
| [SelectionToolProtocol](agent/core/domain/selection_protocol.py) | `object_dict()`, `extract_message()`, `call_ids_valid()`, `has_mixed_finish()`, `parse_arguments()` |
| [AttemptState](agent/core/domain/state.py) | `init()`, `remaining()`, `ensure_current()`, `target_remaining()`, `must_force_finish()` |
| [MemorySummary](agent/core/domain/summary.py) | `from_context()` |
| [MemorySummaryInput](agent/core/domain/summary_input.py) | `_has_content()`, `_compact_content()`, `_content_lines()`, `_memory_text()`, `_dialogue_only()`, `build()` |
| [ToolSpec](agent/core/domain/tool.py) | `name`, `input_model`, `store_method`, `result_key`, `max_calls`, `admission_checks` |

### retrieval

| 类 | 声明的方法或字段 |
| --- | --- |
| [RecallDocument](retrieval/core/domain/document.py) | `to_recall_payload()` |
| [RecallDocumentSnapshot](retrieval/core/domain/document_snapshot.py) | `__init__()`, `_stamp()`, `_day_interval()`, `time_context()`, `coverage()`, `filter_by_time()` |
| [ReadLedger](retrieval/core/domain/ports.py) | `register_candidate()`, `register_read()`, `record_retrievals()` |
| [MemoryReadPort](retrieval/core/domain/ports.py) | `async load_records()`, `async temporal_coverage()`, `async search()`, `async read()`, `async list_memories()` |
| [MemoryReaderFactory](retrieval/core/domain/ports.py) | `create()` |
| [MemoryDocumentRepository](retrieval/core/domain/ports.py) | `async load()` |
| [MemorySearchRepository](retrieval/core/domain/ports.py) | `async search()` |
| [ReadSessionSeed](retrieval/core/domain/ports.py) | `recent_update_memory`, `recent_retrievals` |
| [RecallHit](retrieval/core/domain/retrieval.py) | `ref`, `score` |
| [Status](retrieval/core/domain/tools/common.py) | — |
| [TimeContext](retrieval/core/domain/tools/common.py) | `conversation_time`, `memory_date`, `record_dates`, `time_precision` |
| [TopicTimeContext](retrieval/core/domain/tools/common.py) | `event_time_status`, `event_intervals`, `unknown_event_count`, `plan_dates` |
| [ToolError](retrieval/core/domain/tools/common.py) | `code`, `message` |
| [RecallSearchTerms](retrieval/core/domain/tools/common.py) | `normalize()` |
| [TimeRange](retrieval/core/domain/tools/common.py) | `_ordered_and_aware()` |
| [ListCoverage](retrieval/core/domain/tools/listing.py) | `scope_handle`, `includes_hot_and_cold`, `filter_target`, `scan_complete`, `result_limit_reached`, `response_budget_reached`, `temporal` |
| [ListItem](retrieval/core/domain/tools/listing.py) | `card_handle`, `read_handle`, `kind`, `title`, `time_context`, `sections` |
| [ListResult](retrieval/core/domain/tools/listing.py) | `status`, `coverage`, `items`, `errors` |
| [ListInput](retrieval/core/domain/tools/listing.py) | `_lists_none_to_empty()`, `_keywords_clean()`, `_kinds_default_full()`, `_clue_strip()`, `_fallback_needs_clue()` |
| [ReadError](retrieval/core/domain/tools/read.py) | `card_handle` |
| [ReadEntry](retrieval/core/domain/tools/read.py) | `read_handle`, `card_handle`, `kind`, `title`, `metadata`, `sections`, `time_context` |
| [ReadResult](retrieval/core/domain/tools/read.py) | `status`, `reads`, `errors` |
| [ReadInput](retrieval/core/domain/tools/read.py) | `_handles_clean()` |
| [SearchCoverage](retrieval/core/domain/tools/search.py) | `scope_handle`, `search_target`, `literal_scan_complete`, `semantic_query_completed`, `result_limit_reached`, `response_budget_reached`, `temporal` |
| [SearchItem](retrieval/core/domain/tools/search.py) | `card_handle`, `read_handle`, `kind`, `title`, `matched_by`, `time_context`, `sections` |
| [SearchResult](retrieval/core/domain/tools/search.py) | `status`, `coverage`, `items`, `errors` |
| [SearchInput](retrieval/core/domain/tools/search.py) | `_lists_none_to_empty()`, `_terms_clean()`, `_kinds_default_full()`, `_query_strip()`, `_mode_fields()` |

### cache

| 类 | 声明的方法或字段 |
| --- | --- |
| [RecallCache](cache/core/domain/cache.py) | `version`, `signal_seq`, `recent_update_memory`, `recent_retrievals`, `refresh_complete` |
| [RecallCacheView](cache/core/domain/cache.py) | `cache`, `latest_signal_seq`, `changes`, `recent_updates` |
| [RecallCachePort](cache/core/domain/ports.py) | `async load()`, `async save()` |
| [MemoryUpdate](cache/core/domain/update.py) | `document_id`, `kind`, `revision`, `content_hash`, `deleted` |

### shared

| 类 | 声明的方法或字段 |
| --- | --- |
| [MaterialTokenCounter](shared/core/domain/budgeting.py) | `count_text()` |
| [RecallMaterialBudget](shared/core/domain/budgeting.py) | `count_tokens()` |
| [ErrorSpec](shared/core/domain/errors.py) | `build()` |
| [MemoryRecallUnavailable](shared/core/domain/errors.py) | — |
| [VisibleItem](shared/core/domain/records.py) | `card_handle`, `read_handle`, `title`, `kind`, `sections`, `matched_by`, `time_context` |
| [ReadRecord](shared/core/domain/records.py) | `document_id`, `kind`, `title`, `sections`, `revision`, `content_hash`, `conversation_time`, `time_context` |
| [RecentRecord](shared/core/domain/records.py) | `card_handle`, `read_handle` |

## Processing 业务执行和编排

### agent

| 类 | 声明的方法或字段 |
| --- | --- |
| [MemoryRecallConfig](agent/core/processing/config/pipeline.py) | `__post_init__()` |
| [MemorySelectionService](agent/core/processing/selection/service.py) | `async select()`, `initial_search()`, `__init__()`, `validate_args()`, `async execute_tool()`, `validate_finish()`, `_policy()`, `_recent_item()`, `build_l0()`, `_unfinished()`, `async _request_model()`, `async _dispatch_call()`, `async run()` |
| [MemoryRecallService](agent/core/processing/service.py) | `__init__()`, `async prepare()`, `project_chat_context()`, `async _run_pipeline()` |
| [MemorySummaryService](agent/core/processing/summary/service.py) | `__init__()`, `async summarize()` |
| [ToolExecutor](agent/core/processing/tools/executor.py) | `_merge_recent()`, `__init__()`, `_validate()`, `validate_args()`, `register_visible()`, `_register_visible_items()`, `record_result()`, `_fit_output()`, `async _call_store()`, `async execute_tool()` |
| [RecallToolRegistry](agent/core/processing/tools/registry.py) | `_check_search()`, `_check_read()`, `_check_list()` |
| [RecallToolInputValidator](agent/core/processing/tools/validation.py) | `schema_errors()` |

### retrieval

| 类 | 声明的方法或字段 |
| --- | --- |
| [MemoryReadService](retrieval/core/processing/service.py) | `_hit_count()`, `_occurrence_count()`, `_match_hit()`, `_time_key()`, `__init__()`, `_in_scope()`, `_pin_payload()`, `async _load_payload()`, `async load_records()`, `async temporal_coverage()`, `async _time_candidates()`, `async search()`, `async read()`, `async list_memories()`, `_summary_sections()`, `_time_context()`, `async _document_snapshot()`, `async _semantic_recall()`, `_retrieval_entry()`, `_tokens()` |
| [MemoryReadSession](retrieval/core/processing/session.py) | `__init__()`, `_restore()`, `async temporal_coverage()`, `async search()`, `async read()`, `async list_memories()`, `record_retrievals()`, `recent_retrievals()`, `materialize()`, `register_candidate()`, `register_read()` |

### cache

| 类 | 声明的方法或字段 |
| --- | --- |
| [MemoryCacheService](cache/core/processing/service.py) | `__init__()`, `async prepare()`, `async remember()` |

## Repository 持久层与外部适配

### agent

| 类 | 声明的方法或字段 |
| --- | --- |
| [CompatibleMemoryModelGateway](agent/repository/llm.py) | `__init__()`, `async complete()` |
| [MemoryRecallResourceLoader](agent/repository/resources.py) | `load_skill()`, `load()` |

### retrieval

| 类 | 声明的方法或字段 |
| --- | --- |
| [QdrantMemorySearchRepository](retrieval/repository/qdrant.py) | `__init__()`, `async search()` |
| [RecallReaderFactory](retrieval/repository/reader_factory.py) | `__init__()`, `create()` |
| [SQLiteMemoryDocumentRepository](retrieval/repository/sqlite.py) | `__init__()`, `async load()` |

### cache

| 类 | 声明的方法或字段 |
| --- | --- |
| [SQLiteRecallCache](cache/repository/sqlite.py) | `__init__()`, `async load()`, `async save()`, `_update()` |
