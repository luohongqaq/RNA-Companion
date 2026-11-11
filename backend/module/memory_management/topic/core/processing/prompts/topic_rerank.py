"""去重后的完整topic候选只打分一次，不输入时间线。"""
from __future__ import annotations


TOPIC_RERANK_INSTRUCTION = (
    "Determine whether the object record in the Query belongs to the specific ongoing "
    "affair documented by the topic in the Document. The object includes its title and "
    "description. The topic includes its title, identity boundary, and current state. "
    "Use the identity boundary to distinguish affairs. Different stages, new progress, "
    "changes, supplements, or corrections within the same affair are matches even when "
    "the current states differ. Sharing a person, organization, broad domain, generic "
    "goal, or related concern alone is insufficient. Do not invent missing connections."
)
