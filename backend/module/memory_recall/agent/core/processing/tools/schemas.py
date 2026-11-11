"""工具 Schema：模型侧工具契约，与 SKILL.md 对齐。

分工：SKILL.md 教"什么时候用哪个"；这里声明"这个工具是什么、参数怎么填"；
Python 校验强制。数值即契约：literal_terms/keywords 最多 4、search limit 1~10、
list limit 1~10、card_handles 最多 5、selected 最多 10 项、unresolved 最多 3 项，
改动需 SKILL.md、Schema、Python 校验三方同步。
跨字段规则（grep/hybrid 必须给非空 literal_terms、fallback 必须给
history_clue、reason_code 与 selected 的一致性等）无法用 JSON Schema 表达，
由 agent 的参数校验落实。工具的输出契约（status/coverage/items/pre_read/
reads/errors 结构）写在 memory_store / agent 的函数说明里，不进 Schema。
"""
from __future__ import annotations


SCOPE_ROOTS = {
    "search_scope": {"handle": "scope_hot", "description": "热池：search_memories 的唯一合法搜索范围。"},
    "list_scope": {"handle": "scope_all", "description": "list_memories 的授权范围；概览查热池，回捞查冷池。"},
}


SECTION_KEYS = [
    "memorable_moments", "profile_facts", "intentions", "commitments", "reflections",
    "topic_identity", "current_state", "entries",
]


SEARCH_MEMORIES = {
    "type": "function",
    "function": {
        "name": "search_memories",
        "description": (
            "查找正式历史记忆，支持 grep 字面、semantic 语义和 hybrid 双路检索。"
            "先仅据当前对话确定历史目标；没有目标或只有对话信息缺口时直接finish_selection，不能由候选反推目标。"
            "当历史有助于回答问题、理解用户或自然接话，且当前上下文与已读材料不足、"
            "RECENT_RETRIEVALS 的 recent_update_memory 和 recent_retrievals 中的卡片无法直接定位所需材料时使用。"
            "同一目标近期已搜索过时优先复用；仅在出现新的查找方面、线索、"
            "需要补足的覆盖缺口或材料失效时追加搜索。"
            "结果含read_handle及sections即为可直接选择的已核验证据，无需再read使其生效；"
            "只有标题或read_handle为空的候选才需要读取后获得正文证据。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "scope_handle": {
                    "type": "string",
                    "minLength": 1,
                    "enum": ["scope_hot"],
                    "description": "固定填写 scope_hot，取自 ROOTS.search_scope.handle",
                },
                "purpose": {
                    "type": "string",
                    "minLength": 1,
                    "maxLength": 200,
                    "description": (
                        "本次希望补充的历史事实或背景及其用途，"
                        "用于说明本次查找目标；调用日志由后端管理。"
                        "例如：了解用户过去项目的选择动机，以便接续当前话题。"
                    ),
                },
                "mode": {
                    "type": "string",
                    "enum": ["grep", "semantic", "hybrid"],
                    "description": (
                        "grep=字面子串匹配，必须提供 literal_terms；"
                        "semantic=语义近似召回，必须提供 query；"
                        "hybrid=两路并行合并，必须同时提供 literal_terms 和 query。"
                    ),
                },
                "literal_terms": {
                    "type": "array",
                    "minItems": 1,
                    "maxItems": 4,
                    "uniqueItems": True,
                    "items": {
                        "type": "string",
                        "minLength": 1,
                    },
                    "description": (
                        "grep/hybrid 必填：1～4 个非空的短字面锚点，"
                        "如原词、名称或标识；不支持正则表达式。"
                        "没有可靠字面锚点时使用 semantic 并省略此字段。"
                    ),
                },
                "match": {
                    "type": "string",
                    "enum": ["any", "all"],
                    "description": (
                        "仅用于 grep/hybrid，省略时为 any。"
                        "any=任一词命中，命中次数多的文档排序靠前；"
                        "all=全部词在同一文档内命中（不要求同一行），按时间降序。"
                        "any 用于同一查找目标的多个字面锚点，也可用于共同回顾或比较的几个项目；"
                        "all 用于需要几个侧面共现才锁定的同一事件；"
                        "要找几件不同的事应分开调用，不用 any 打包。"
                        "semantic 模式省略此字段。"
                    ),
                },
                "query": {
                    "type": "string",
                    "minLength": 1,
                    "maxLength": 600,
                    "description": (
                        "semantic/hybrid 必填且非空："
                        "描述希望查找的历史事实、经历或背景，最多 600 字符。"
                        "grep 模式省略此字段。"
                    ),
                },
                "kinds": {
                    "type": "array",
                    "minItems": 1,
                    "maxItems": 2,
                    "uniqueItems": True,
                    "items": {
                        "type": "string",
                        "enum": ["daily", "topic"],
                    },
                    "description": (
                        "搜索的记忆类型。省略时同时搜索 daily 和 topic；"
                        "实际范围仍受 scope_handle 的授权限制。"
                    ),
                },
                "limit": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": 10,
                    "description": (
                        "每个记忆类型的卡片数上限，1～10，缺省为 10；"
                        "两类都搜时每类最多 10 张。"
                        "每类排序前 5 张附带正式摘要分区，其余仅返回标题；"
                        "需要细节时用 card_handle 调用 read_memories。"
                    ),
                },
                "time_range": {
                    "type": "object",
                    "properties": {
                        "basis": {
                            "type": "string",
                            "enum": ["conversation_time", "event_time"],
                            "description": (
                                "conversation_time=记忆日期或得知日期（后端当地日历日，不代表精确交流时刻）；"
                                "event_time=记忆中已明确记录的事件时间。"
                                "不得使用文档更新时间替代。"
                            ),
                        },
                        "start": {
                            "type": "string",
                            "format": "date-time",
                            "description": (
                                "范围起点，包含该时刻。"
                                "使用带时区偏移的 ISO 8601 时间。"
                            ),
                        },
                        "end": {
                            "type": "string",
                            "format": "date-time",
                            "description": (
                                "范围终点，不包含该时刻，必须晚于 start。"
                                "使用带时区偏移的 ISO 8601 时间。"
                            ),
                        },
                    },
                    "required": ["basis", "start", "end"],
                    "additionalProperties": False,
                    "description": (
                        "可选时间范围。conversation_time筛选记忆日期或得知日期（后端当地日历日）；event_time只筛选topic明确记录的已发生事件，"
                        "不把daily日期或topic听说时间当事件时间。范围左闭右开。"
                    ),
                },
                "unknown_event_time": {
                    "type": "string",
                    "enum": ["include", "exclude"],
                    "description": (
                        "仅event_time筛选使用；未知事件时间的topic默认include保留，exclude排除。"
                        "排除未知后的空结果不能证明该时段没有事件。"
                    ),
                },
            },
            "required": ["scope_handle", "purpose", "mode"],
            "additionalProperties": False,
        },
    },
}


READ_MEMORIES = {
    "type": "function",
    "function": {
        "name": "read_memories",
        "description": (
            "读取指定候选的完整记忆，补充当前目标所需、摘要尚未展示的信息。"
            "适用于高度相关的标题候选，或需要确认具体经过、事件阶段、意义等已有线索指向的细节。"
            "搜索和列举返回的摘要已可选择；不为激活read_handle、换成全文更保险或机械补情绪而读取。"
            "同一版本全文可复用时不重复读，读后仅选择有用分区。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "purpose": {
                    "type": "string",
                    "minLength": 1,
                    "maxLength": 200,
                    "description": (
                        "简要说明摘要还缺什么、候选哪条线索提示正文可能补足，及它如何改变选材。"
                        "允许有依据的探索，不要求预先保证命中；不能只写“了解更多”或“确认句柄有效”。"
                    ),
                },
                "card_handles": {
                    "type": "array",
                    "minItems": 1,
                    "maxItems": 5,
                    "uniqueItems": True,
                    "items": {
                        "type": "string",
                        "minLength": 1,
                    },
                    "description": (
                        "需要阅读全文的候选卡片句柄，最多 5 个。"
                        "来自本轮有效的 RECENT_RETRIEVALS.recent_update_memory、recent_retrievals "
                        "或工具返回结果。"
                        "不得自行构造句柄或填写文档 ID。"
                    ),
                },
            },
            "required": ["purpose", "card_handles"],
            "additionalProperties": False,
        },
    },
}


LIST_MEMORIES = {
    "type": "function",
    "function": {
        "name": "list_memories",
        "description": (
            "按时间、关键词及类型筛选并返回有限正式记忆摘要。概览只查热池，回捞只查冷池。"
            "仅用于事件概览，或有明确既往交流线索时的最后查找。"
            "overview 可直接调用；fallback 要求已有材料和热池搜索仍不足，"
            "且没有可直接读取的候选。"
            "热池无命中本身不能触发 fallback。"
            "含read_handle和sections的正式摘要可直接提交，不要求调用read_memories。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "scope_handle": {
                    "type": "string",
                    "minLength": 1,
                    "enum": ["scope_all"],
                    "description": (
                        "固定填写 scope_all，取自 ROOTS.list_scope.handle；不得填写 description 的中文说明。"
                        "由 mode 决定在授权范围内查热池还是冷池。"
                        "不得使用搜索工具的句柄，"
                        "也不得自行构造或扩大授权。"
                    ),
                },
                "mode": {
                    "type": "string",
                    "enum": ["overview", "fallback"],
                    "description": (
                        "overview=从热池列举历史事件或话题概览；"
                        "fallback=已有材料和热池搜索不足后，"
                        "依据明确的既往交流线索在冷池进行最后查找。"
                    ),
                },
                "purpose": {
                    "type": "string",
                    "minLength": 1,
                    "maxLength": 200,
                    "description": (
                        "本次希望列举或找回什么，以及用途。"
                        "例如：列举用户上个月聊过的项目经历。"
                    ),
                },
                "history_clue": {
                    "type": "string",
                    "minLength": 1,
                    "maxLength": 300,
                    "description": (
                        "fallback 必填：说明此前交流过这段历史的具体线索及来源。"
                        "例如：用户本轮明确说“之前跟你讲过这个项目”。"
                        "不得仅填写“热池没搜到”，不得编造历史依据。"
                        "overview 模式省略。"
                    ),
                },
                "time_range": {
                    "type": "object",
                    "properties": {
                        "basis": {
                            "type": "string",
                            "enum": ["conversation_time", "event_time"],
                            "description": (
                                "conversation_time=记忆日期或得知日期（后端当地日历日，不代表精确交流时刻）；"
                                "event_time=记忆中已明确记录的事件时间。"
                                "不得使用文档更新时间替代。"
                            ),
                        },
                        "start": {
                            "type": "string",
                            "format": "date-time",
                            "description": (
                                "范围起点，包含该时刻。"
                                "使用带时区偏移的 ISO 8601 时间。"
                            ),
                        },
                        "end": {
                            "type": "string",
                            "format": "date-time",
                            "description": (
                                "范围终点，不包含该时刻，必须晚于 start。"
                                "使用带时区偏移的 ISO 8601 时间。"
                            ),
                        },
                    },
                    "required": ["basis", "start", "end"],
                    "additionalProperties": False,
                    "description": (
                        "可选时间范围。conversation_time筛选记忆日期或得知日期（后端当地日历日）；event_time只筛选topic明确记录的已发生事件，"
                        "不把daily日期或topic听说时间当事件时间。范围左闭右开。"
                    ),
                },
                "keywords": {
                    "type": "array",
                    "minItems": 1,
                    "maxItems": 4,
                    "uniqueItems": True,
                    "items": {
                        "type": "string",
                        "minLength": 1,
                    },
                    "description": (
                        "可选的 1～4 个短关键词，"
                        "在标题和正式摘要中做大小写不敏感的字面子串匹配。"
                        "不支持正则或语义扩展；省略时不按关键词筛选。"
                    ),
                },
                "match": {
                    "type": "string",
                    "enum": ["any", "all"],
                    "description": (
                        "仅在提供 keywords 时使用，缺省为 any。"
                        "any=任一关键词命中；"
                        "all=全部关键词在同一文档的标题与摘要合并文本中命中。"
                    ),
                },
                "kinds": {
                    "type": "array",
                    "minItems": 1,
                    "maxItems": 2,
                    "uniqueItems": True,
                    "items": {
                        "type": "string",
                        "enum": ["daily", "topic"],
                    },
                    "description": (
                        "列举的记忆类型，缺省为 daily 和 topic。"
                        "不允许空数组。"
                    ),
                },
                "limit": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": 10,
                    "description": (
                        "本次最多返回的文档数，缺省为 10。"
                        "不代表全部匹配数量；摘要总返回量仍受服务端预算限制。"
                    ),
                },
                "unknown_event_time": {
                    "type": "string",
                    "enum": ["include", "exclude"],
                    "description": (
                        "仅event_time筛选使用；未知事件时间的topic默认include保留，exclude排除。"
                        "排除未知后的空结果不能证明该时段没有事件。"
                    ),
                },
            },
            "required": ["scope_handle", "mode", "purpose"],
            "additionalProperties": False,
        },
    },
}


FINISH_SELECTION = {
    "type": "function",
    "function": {
        "name": "finish_selection",
        "description": (
            "提交最终记忆选择并结束本轮选择流程。"
            "没有需要历史补充的目标、已读材料足够、明确目标未找到可用材料，"
            "或无法继续查找时调用。"
            "只引用实际提供的read_handle和sections；search/list的摘要与read的全文均可引用，"
            "不提交自行编写的记忆正文。"
            "后端校验成功后直接结束循环，不再请求模型确认。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "reason_code": {
                    "type": "string",
                    "enum": [
                        "context_sufficient",
                        "selected",
                        "no_relevant_memory",
                        "incomplete",
                    ],
                    "description": (
                        "按顺序判定：没有需要历史补充的目标（包括只有对话信息缺口）用context_sufficient，selected和unresolved均为空，不要求对话信息完整；"
                        "有明确历史目标但因执行失败、预算耗尽或必要证据问题未完成用incomplete；"
                        "材料足够用selected，selected非空；"
                        "有明确历史目标、查找已结束但未找到或选出可用材料用no_relevant_memory，selected为空。"
                        "局部失败而其他材料已足够用selected；正常无命中不等于incomplete。不要按下游如何回复判断状态。"
                    ),
                },
                "selected": {
                    "type": "array",
                    "maxItems": 10,
                    "items": {
                        "type": "object",
                        "properties": {
                            "read_handle": {
                                "type": "string",
                                "minLength": 1,
                                "description": (
                                    "已提供内容的证据引用：search/list随摘要签发，read随全文签发。"
                                    "直接使用工具返回的read_handle，无需再read使摘要引用生效；card_handle不能替代。"
                                ),
                            },
                            "section_keys": {
                                "type": "array",
                                "minItems": 1,
                                "uniqueItems": True,
                                "items": {
                                    "type": "string",
                                    "enum": SECTION_KEYS,
                                },
                                "description": (
                                    "该read_handle对应工具结果中sections实际出现的键。"
                                    "例如只有memorable_moments就只可选它，不能选未返回的profile_facts或entries；"
                                    "这些已返回的摘要键无需再读全文即可提交。"
                                ),
                            },
                        },
                        "required": ["read_handle", "section_keys"],
                        "additionalProperties": False,
                    },
                    "description": (
                        "最终选择的材料，最多 10 项，仍受服务端文档数和 token 预算限制。"
                        "无需记忆或没有有用材料时为空数组。"
                        "不得重复选择同一文档的同一分区。"
                    ),
                },
                "unresolved": {
                    "type": "array",
                    "maxItems": 3,
                    "items": {
                        "type": "string",
                        "minLength": 1,
                        "maxLength": 300,
                    },
                    "description": (
                        "仅记录本轮已确定的历史目标仍缺的证据或影响选材的冲突，说明目标与缺证据。"
                        "如明确旧事未定位、必要证据因失败未取得；其他来源已足够时不因局部失败自动留项。"
                        "不列用户未展开的新细节、未来未知、假设性原因或已由时间/来源字段表达的限制。"
                        "当前用户给出的新进展作为当前证据，不要求旧记忆再确认。没有实际历史缺口时填[]。"
                    ),
                },
            },
            "required": ["reason_code", "selected", "unresolved"],
            "additionalProperties": False,
        },
    },
}


TOOLS = [SEARCH_MEMORIES, READ_MEMORIES, LIST_MEMORIES, FINISH_SELECTION]
