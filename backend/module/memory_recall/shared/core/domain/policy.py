"""读取工具和跨轮材料复用共同遵守的数量与内容上限。

集中定义单篇正文限制、近期检索条数、近期更新条数及每类摘要卡片数。
Agent 展示、检索返回和缓存持久化使用相同取值，避免不同环节的材料范围发生漂移。
"""
from __future__ import annotations


SINGLE_DOC_TOKEN_LIMIT = 4000


RECENT_LIMIT = 10


RECENT_UPDATE_LIMIT = 5


SUMMARY_CARDS_PER_KIND = 5
