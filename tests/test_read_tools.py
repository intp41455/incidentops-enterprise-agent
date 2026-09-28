"""两个只读工具的行为测试。

覆盖：正常诊断、参数校验、日志脱敏、故障由数据触发、以及
"对模型暴露的入口不含身份参数"这一结构性约束。
"""

from __future__ import annotations

import inspect
import json

import pytest

from business_sim.tools import (
    DEFAULT_LOG_LIMIT,
    ERR_INVALID_ARGUMENT,
    ERR_NOT_FOUND_OR_FORBIDDEN,
    ERR_TIMEOUT,
    build_tool_registry,
    get_import_job,
    get_job_logs,
)


# --- get_import_job ---------------------------------------------------------


def test_import_job_104_returns_invalid_date_fact(session, ctx_a_viewer):
    result = get_import_job(session, ctx_a_viewer, "IMP-104")

    assert result["ok"] is True
    assert result["error"] is None
    assert result["retryable"] is False

    data = result["data"]
    assert data["job_id"] == "IMP-104"
    assert data["status"] == "failed"
    assert data["error_code"] == "INVALID_DATE"
    assert data["file_id"] == "file-104"
    assert data["filename"] == "roster_2026_09.csv"
    assert data["manual_version"] == "v1"


def test_import_job_evidence_is_locatable(session, ctx_a_viewer):
    result = get_import_job(session, ctx_a_viewer, "IMP-104")

    evidence_ids = {item["id"] for item in result["evidence"]}
    assert "ev-job-IMP-104-v3" in evidence_ids
    assert "ev-file-file-104" in evidence_ids

    for item in result["evidence"]:
        assert item["source_type"] in {"job", "file", "log"}
        assert item["source_id"]


@pytest.mark.parametrize("bad_job_id", ["", "   ", "IMP", "abc-104", "IMP-104; DROP TABLE"])
def test_import_job_rejects_invalid_id(session, ctx_a_viewer, bad_job_id):
    result = get_import_job(session, ctx_a_viewer, bad_job_id)

    assert result["ok"] is False
    assert result["error"]["code"] == ERR_INVALID_ARGUMENT
    assert result["data"] is None
    assert result["evidence"] == []


def test_import_job_unknown_id_is_not_found(session, ctx_a_viewer):
    result = get_import_job(session, ctx_a_viewer, "IMP-999")

    assert result["ok"] is False
    assert result["error"]["code"] == ERR_NOT_FOUND_OR_FORBIDDEN


# --- get_job_logs -----------------------------------------------------------


def test_job_logs_contains_root_cause_row(session, ctx_a_viewer):
    result = get_job_logs(session, ctx_a_viewer, "IMP-104")

    assert result["ok"] is True
    data = result["data"]
    assert data["count"] == 5
    assert data["total"] == 5
    assert data["truncated"] is False

    messages = " ".join(item["message"] for item in data["logs"])
    assert "第 3 行校验失败" in messages
    assert "start_date" in messages


def test_job_logs_masks_secret_values(session, ctx_a_viewer):
    result = get_job_logs(session, ctx_a_viewer, "IMP-104")

    payload = json.dumps(result, ensure_ascii=False)
    # 日志原文里的占位凭据不得原样返回
    assert "syn-placeholder-not-a-real-secret" not in payload
    assert "token=***" in payload


def test_job_logs_truncates_and_reports_total(session, ctx_a_viewer):
    result = get_job_logs(session, ctx_a_viewer, "IMP-104", limit=2)

    assert result["ok"] is True
    assert result["data"]["count"] == 2
    assert result["data"]["total"] == 5
    assert result["data"]["truncated"] is True


@pytest.mark.parametrize("bad_limit", [0, -1, 101, 1000, "10", 1.5, True])
def test_job_logs_rejects_invalid_limit(session, ctx_a_viewer, bad_limit):
    result = get_job_logs(session, ctx_a_viewer, "IMP-104", limit=bad_limit)

    assert result["ok"] is False
    assert result["error"]["code"] == ERR_INVALID_ARGUMENT


def test_job_logs_default_limit_is_used(session, ctx_a_viewer):
    result = get_job_logs(session, ctx_a_viewer, "IMP-104")

    assert result["ok"] is True
    assert len(result["data"]["logs"]) <= DEFAULT_LOG_LIMIT


def test_job_logs_timeout_does_not_fabricate_content(session, ctx_a_viewer):
    """IMP-107 的日志查询由数据表注入 TIMEOUT，工具必须明确失败。"""
    result = get_job_logs(session, ctx_a_viewer, "IMP-107")

    assert result["ok"] is False
    assert result["error"]["code"] == ERR_TIMEOUT
    assert result["retryable"] is True
    assert result["data"] is None
    assert result["evidence"] == []


def test_job_logs_unknown_job_is_not_found(session, ctx_a_viewer):
    result = get_job_logs(session, ctx_a_viewer, "IMP-999")

    assert result["ok"] is False
    assert result["error"]["code"] == ERR_NOT_FOUND_OR_FORBIDDEN


# --- 对模型暴露的入口 -------------------------------------------------------


def test_registry_hides_identity_parameters(session, ctx_a_viewer):
    registry = build_tool_registry(session, ctx_a_viewer)

    assert set(registry) >= {"get_import_job", "get_job_logs", "validate_csv", "check_prerequisites", "get_ticket_status"}
    for name, fn in registry.items():
        params = set(inspect.signature(fn).parameters)
        assert not (params & {"tenant_id", "user_id", "role", "principal", "ctx", "session"}), f"{name} 暴露了身份/上下文参数：{sorted(params)}"



def test_registry_rejects_identity_injection(session, ctx_a_viewer):
    registry = build_tool_registry(session, ctx_a_viewer)

    with pytest.raises(TypeError):
        registry["get_import_job"](job_id="IMP-104", tenant_id="tenant_b")

    with pytest.raises(TypeError):
        registry["get_job_logs"](job_id="IMP-104", role="approver")


def test_registry_calls_through_to_tools(session, ctx_a_viewer):
    registry = build_tool_registry(session, ctx_a_viewer)

    result = registry["get_import_job"](job_id="IMP-104")
    assert result["ok"] is True
    assert result["data"]["error_code"] == "INVALID_DATE"
