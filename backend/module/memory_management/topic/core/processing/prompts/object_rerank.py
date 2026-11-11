"""Object事件主题归属重排；名称是判断事件主题身份的一部分，不能省略。"""
from __future__ import annotations


OBJECT_NAME_RERANK_INSTRUCTION = (
    "Determine whether the two titles refer to the same specific affair, project, or event. "
    "Allow different wording or abbreviations for the same affair. Shared words, a person, "
    "an organization, a broad category, or a generic goal alone do not establish identity. "
    "Use only the supplied titles; do not invent missing details or assume that distinct "
    "affairs are identical merely because they are related."
)


OBJECT_CONTENT_RERANK_INSTRUCTION = (
    "Determine whether the two records, each containing a title and a description, concern "
    "the same specific ongoing affair, project, or event. Use the titles to identify the "
    "affair and the descriptions to check its scope and continuity. Different stages, "
    "later progress, changes, supplements, or corrections of the same affair are matches. "
    "Sharing a person, organization, domain, generic goal, or related concerns alone is "
    "insufficient. Do not infer a shared affair from missing context."
)
