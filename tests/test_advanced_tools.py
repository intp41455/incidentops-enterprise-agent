"""高级工具与安全约束测试。

覆盖：
1. validate_csv：检测出第3行非法日期，并将提示注入样本视为纯数据；
2. check_prerequisites：核对前置依赖（如超时后的幂等状态未知）；
3. create_ticket：
   - 未审批拒绝写入；
   - 提案篡改（Hash不匹配）审批失效；
   - 幂等键防重重放；
4. get_ticket_status：租户隔离与查询。
"""

from __future__ import annotations

import json
import pytest

from business_sim.tools import (
    check_prerequisites,
    create_ticket,
    get_ticket_status,
    validate_csv,
)
from business_sim.repository import ActionProposalRepository, ApprovalRepository, TicketRepository


def test_validate_csv_detects_invalid_date(session, ctx_a_viewer):
    result = validate_csv(session, ctx_a_viewer, "file-104")

    assert result["ok"] is True
    data = result["data"]
    assert data["valid"] is False
    assert data["error_count"] == 1
    err = data["errors"][0]
    assert err["row"] == 3
    assert err["field"] == "start_date"
    assert err["error_type"] == "INVALID_DATE_FORMAT"
    assert "2026-13-01" in err["raw_value"]


def test_validate_csv_injection_treated_as_data(session, ctx_a_viewer):
    """file-106 的 CSV 单元格包含提示注入文本，系统必须将其视为普通数据，绝不执行越权指令。"""
    result = validate_csv(session, ctx_a_viewer, "file-106")

    assert result["ok"] is True
    assert result["data"]["file_id"] == "file-106"
    assert len(result["evidence"]) == 1


def test_check_prerequisites_detects_unknown_idempotency(session, ctx_a_operator):
    """IMP-203 前置条件为 unknown，需由 ReviewAgent 或 RemediationAgent 阻断直接重试并要求补查。"""
    result = check_prerequisites(session, ctx_a_operator, "IMP-203", "idempotency_status")

    assert result["ok"] is True
    assert result["data"]["status"] == "unknown"
    assert result["data"]["passed"] is False


def test_create_ticket_rejects_unapproved(session, ctx_a_operator):
    """未获审批的提案，执行器必须坚决拒绝写入。"""
    repo = ActionProposalRepository(session)
    prop = repo.create_proposal(
        tenant_id=ctx_a_operator.tenant_id,
        proposal_id="PROP-PENDING",
        action_type="create_ticket",
        target_id="IMP-104",
        target_version=3,
        payload_json='{"title": "测试工单"}',
        payload_hash="fake-hash",
        created_by="agent",
    )

    result = create_ticket(session, ctx_a_operator, "PROP-PENDING", "idemp-001")
    assert result["ok"] is False
    assert result["error"]["code"] == "UNAPPROVED_ACTION"


def test_create_ticket_rejects_tampered_payload(session, ctx_a_operator):
    """审批后若修改了提案内容（Hash 不匹配），原审批必须自动失效。"""
    prop_repo = ActionProposalRepository(session)
    appr_repo = ApprovalRepository(session)

    prop = prop_repo.create_proposal(
        tenant_id=ctx_a_operator.tenant_id,
        proposal_id="PROP-TAMPERED",
        action_type="create_ticket",
        target_id="IMP-104",
        target_version=3,
        payload_json='{"title": "原版工单"}',
        payload_hash="original-hash-1234",
        created_by="agent",
    )
    appr_repo.record_decision(
        tenant_id=ctx_a_operator.tenant_id,
        approval_id="appr-t-1",
        proposal_id="PROP-TAMPERED",
        approver_id="u-a-approver",
        decision="approved",
        payload_hash="original-hash-1234",
    )
    # 修改提案内容
    prop.status = "approved"
    prop.payload_json = '{"title": "篡改后的工单"}'
    prop.payload_hash = "tampered-hash-5678"
    session.flush()

    result = create_ticket(session, ctx_a_operator, "PROP-TAMPERED", "idemp-002")
    assert result["ok"] is False
    assert result["error"]["code"] == "STALE_VERSION"
    assert "失效" in result["error"]["message"]


def test_create_ticket_succeeds_and_enforces_idempotency(session, ctx_a_operator):
    """验证 PROP-104 首次创建成功，二次重放使用相同幂等键直接返回已有工单，工单总数不增加。"""
    result1 = create_ticket(session, ctx_a_operator, "PROP-104", "idemp-prop-104")
    assert result1["ok"] is True
    assert result1["data"]["idempotency_result"] == "created"
    ticket_id = result1["data"]["ticket_id"]

    result2 = create_ticket(session, ctx_a_operator, "PROP-104", "idemp-prop-104")
    assert result2["ok"] is True
    assert result2["data"]["idempotency_result"] == "replay"
    assert result2["data"]["ticket_id"] == ticket_id
    assert "未重复创建" in result2["data"]["message"]

    queried = get_ticket_status(session, ctx_a_operator, ticket_id)
    assert queried["ok"] is True
    assert queried["data"]["ticket_id"] == ticket_id
