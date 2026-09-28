"""租户隔离测试。

关键断言不是"返回了错误码"，而是**没有跨租户内容返回**、
且**错误消息不泄漏目标是否存在**。
"""

from __future__ import annotations

import pytest

from business_sim.repository import ImportJobRepository, JobLogRepository
from business_sim.tools import (
    ERR_NOT_FOUND_OR_FORBIDDEN,
    get_import_job,
    get_job_logs,
)


def test_cross_tenant_job_is_invisible(session, ctx_b_viewer):
    """B 租户用户查 A 租户的 IMP-104：不得返回任何 A 租户内容。"""
    result = get_import_job(session, ctx_b_viewer, "IMP-104")

    assert result["ok"] is False
    assert result["error"]["code"] == ERR_NOT_FOUND_OR_FORBIDDEN
    assert result["data"] is None
    assert result["evidence"] == []


def test_cross_tenant_error_message_does_not_leak_existence(session, ctx_b_viewer):
    result = get_import_job(session, ctx_b_viewer, "IMP-104")

    message = result["error"]["message"]
    assert "IMP-104" not in message
    assert "tenant_a" not in message
    assert "INVALID_DATE" not in message


def test_cross_tenant_logs_are_invisible(session, ctx_b_viewer):
    """跨租户读日志不得回退成"任务不存在但日志照给"。"""
    result = get_job_logs(session, ctx_b_viewer, "IMP-104")

    assert result["ok"] is False
    assert result["error"]["code"] == ERR_NOT_FOUND_OR_FORBIDDEN
    assert result["data"] is None


def test_each_tenant_sees_only_own_job(session, ctx_a_viewer, ctx_b_viewer):
    """IMP-B01 属于 tenant_b：A 看不到，B 看得到。"""
    denied = get_import_job(session, ctx_a_viewer, "IMP-B01")
    allowed = get_import_job(session, ctx_b_viewer, "IMP-B01")

    assert denied["ok"] is False
    assert denied["error"]["code"] == ERR_NOT_FOUND_OR_FORBIDDEN

    assert allowed["ok"] is True
    assert allowed["data"]["job_id"] == "IMP-B01"


def test_repository_requires_tenant_keyword(session):
    """仓储层无法被调用成"不带租户过滤"的查询。"""
    with pytest.raises(TypeError):
        ImportJobRepository(session).get_job(job_id="IMP-104")

    with pytest.raises(TypeError):
        JobLogRepository(session).list_logs(job_id="IMP-104", limit=10)


def test_repository_filters_even_when_job_id_exists(session):
    """同一个仓储方法，用不同租户查同一 job_id，结果不同。"""
    repo = ImportJobRepository(session)

    assert repo.get_job(tenant_id="tenant_a", job_id="IMP-104") is not None
    assert repo.get_job(tenant_id="tenant_b", job_id="IMP-104") is None


def test_list_jobs_is_scoped_to_tenant(session, ctx_a_viewer, ctx_b_viewer):
    repo = ImportJobRepository(session)

    job_ids_a = {job.job_id for job in repo.list_jobs(tenant_id=ctx_a_viewer.tenant_id)}
    job_ids_b = {job.job_id for job in repo.list_jobs(tenant_id=ctx_b_viewer.tenant_id)}

    assert "IMP-B01" not in job_ids_a
    assert job_ids_b == {"IMP-B01"}
    assert job_ids_a.isdisjoint(job_ids_b)
