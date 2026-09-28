"""OpsDesk 单智能体验证测试。"""

import pytest
from single_agent.agent import OpsDeskAgent


def test_single_agent_normal_imp104(session, ctx_a_viewer):
    agent = OpsDeskAgent(session, ctx_a_viewer)
    res = agent.run("请查 IMP-104 为什么失败")

    assert res["status"] == "succeeded"
    assert len(res["evidence_ids"]) >= 2
    facts_str = " ".join(res["facts"])
    assert "INVALID_DATE" in facts_str
    assert "start_date" in facts_str
    assert res["action_proposal"] is None


def test_single_agent_clarification(session, ctx_a_viewer):
    agent = OpsDeskAgent(session, ctx_a_viewer)
    res = agent.run("导入失败了，帮我看看")

    assert res["status"] == "waiting_user"
    assert res["pending_question"] is not None
    assert "IMP-104" in res["output"]


def test_single_agent_cross_tenant_denied(session, ctx_a_viewer):
    agent = OpsDeskAgent(session, ctx_a_viewer)
    res = agent.run("查看 IMP-B01 的日志")

    assert res["status"] == "needs_human_or_denied"
    assert "IMP-B01" not in res["output"] or "失败" in res["output"]
    assert "tenant_b" not in res["output"]


def test_single_agent_tool_failure_disclosed(session, ctx_a_viewer):
    agent = OpsDeskAgent(session, ctx_a_viewer)
    res = agent.run("查 IMP-107 的错误日志")

    assert res["status"] == "needs_human_or_failed"
    assert "未能取得" in res["output"] or "超时" in res["output"]


def test_single_agent_proposes_ticket_on_request(session, ctx_a_operator):
    agent = OpsDeskAgent(session, ctx_a_operator)
    res = agent.run("IMP-104 导入失败，帮我处理，需要的话创建工单")

    assert res["status"] == "waiting_approval"
    prop = res["action_proposal"]
    assert prop is not None
    assert prop["action_type"] == "create_ticket"
    assert prop["status"] == "pending"
