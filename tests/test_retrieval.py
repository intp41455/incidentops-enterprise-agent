"""规程与知识库检索测试。

覆盖：
1. 关键词与语义检索命中相关规程；
2. 版本隔离：v1 查询不混淆 v2 的新规范；
3. 挂载稳定且可追溯的 evidence ID。
"""

from __future__ import annotations

import pytest
from retrieval.engine import search_runbooks


def test_search_runbooks_finds_invalid_date_guidance(session, ctx_a_viewer):
    res = search_runbooks(session, ctx_a_viewer, query="INVALID_DATE 日期格式异常", product_version="v1")

    assert res["ok"] is True
    assert res["data"]["count"] > 0
    hit_titles = [h["title"] for h in res["data"]["hits"]]
    assert any("INVALID_DATE" in t for t in hit_titles)

    evidence_ids = [e["id"] for e in res["evidence"]]
    assert any("chunk-err-invalid-date" in eid for eid in evidence_ids)


def test_search_runbooks_respects_version_filter(session, ctx_a_viewer):
    """v1 版本检索时不应包含只在 v2 出现的规范（如 dept_code 必填）。"""
    res_v1 = search_runbooks(session, ctx_a_viewer, query="dept_code", product_version="v1")
    assert res_v1["ok"] is True
    # 在 v1 下，过滤掉只适用于 v2 的规程
    hit_ids = [h["chunk_id"] for h in res_v1["data"]["hits"]]
    assert "chunk-csv-v2-fields" not in hit_ids

    res_v2 = search_runbooks(session, ctx_a_viewer, query="dept_code", product_version="v2")
    assert res_v2["ok"] is True
    hit_ids_v2 = [h["chunk_id"] for h in res_v2["data"]["hits"]]
    assert "chunk-csv-v2-fields" in hit_ids_v2


def test_search_runbooks_finds_timeout_retry_prerequisites(session, ctx_a_viewer):
    """搜索超时重试，必须检索出强调'核对幂等状态、严禁盲目重试'的规程。"""
    res = search_runbooks(session, ctx_a_viewer, query="UPSTREAM_TIMEOUT 超时重试", product_version="v1")

    assert res["ok"] is True
    snippets = " ".join([h["snippet"] for h in res["data"]["hits"]])
    assert "严禁在未确认幂等状态前盲目重试" in snippets
    assert "check_prerequisites" in snippets
