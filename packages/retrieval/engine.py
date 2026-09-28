"""规程与知识库检索引擎。

提供带有租户范围与产品版本强制过滤的规程检索工具 search_runbooks。
"""

from __future__ import annotations

import re
from typing import Any, Optional

from sqlalchemy.orm import Session
from business_sim.identity import ToolContext
from .runbooks import RUNBOOK_CHUNKS, RunbookChunk

TOOL_SEARCH_RUNBOOKS = "search_runbooks"


def _tokenize(text: str) -> set[str]:
    # 提取英文词汇及中文字符/词段
    tokens = set(re.findall(r"[A-Za-z0-9_]+|[\u4e00-\u9fa5]", text.lower()))
    return tokens


def search_runbooks(
    session: Session,
    ctx: ToolContext,
    query: str,
    product_version: str = "v1",
    limit: int = 3,
) -> dict[str, Any]:
    """检索企业运维与导入规范手册。

    强制在检索前根据 ctx.tenant_id 和 product_version 进行可见性与版本过滤。
    返回的每一项均挂载稳定可追溯的 evidence ID。
    """
    if not isinstance(query, str) or not query.strip():
        return {
            "ok": False,
            "data": None,
            "evidence": [],
            "error": {"code": "INVALID_ARGUMENT", "message": "query 必须是非空字符串"},
            "retryable": False,
        }

    q_tokens = _tokenize(query)

    scored_chunks: list[tuple[float, RunbookChunk]] = []
    for chunk in RUNBOOK_CHUNKS:
        # 1. 租户过滤：只允许 public 或属于当前租户的规程
        if chunk.tenant_scope not in ("public", ctx.tenant_id):
            continue
        # 2. 版本过滤：只允许 all 或与指定产品版本匹配的规程
        if chunk.product_version not in ("all", product_version):
            continue

        # 3. 相关性打分 (基于 token 命中与 tag 权重)
        score = 0.0
        c_tokens = _tokenize(chunk.title + " " + chunk.content)
        overlap = q_tokens & c_tokens
        score += len(overlap) * 2.0

        for tag in chunk.tags:
            if tag.lower() in q_tokens or any(t in tag.lower() for t in q_tokens):
                score += 3.0

        if query.lower() in chunk.content.lower():
            score += 5.0
        if query.lower() in chunk.title.lower():
            score += 8.0

        if score > 0:
            scored_chunks.append((score, chunk))

    # 按相关度从高到低排序
    scored_chunks.sort(key=lambda x: x[0], reverse=True)
    top_chunks = [chunk for _, chunk in scored_chunks[:limit]]

    hits = [
        {
            "doc_id": c.doc_id,
            "chunk_id": c.chunk_id,
            "title": c.title,
            "product_version": c.product_version,
            "snippet": c.content,
        }
        for c in top_chunks
    ]
    evidence = [
        {
            "id": f"ev-runbook-{c.chunk_id}",
            "source_type": "runbook",
            "source_id": c.chunk_id,
            "version": 1,
        }
        for c in top_chunks
    ]

    return {
        "ok": True,
        "data": {
            "query": query,
            "product_version": product_version,
            "count": len(hits),
            "hits": hits,
        },
        "evidence": evidence,
        "error": None,
        "retryable": False,
    }
