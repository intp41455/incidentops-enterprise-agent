"""多智能体协同架构（IncidentOps）测试。

覆盖：
1. M01 多故障协同排查：两项任务不同根因（格式错误 vs 超时），独立查证，禁止合并；
2. M02 证据缺口与审查退回（REWORK）：超时重试必须先查幂等，触发 ReviewAgent 退回；
3. 不可变提案生成与等待审批状态流转；
4. 租户隔离阻断。
"""

from __future__ import annotations

import pytest
from multi_agent.coordinator import CoordinatorAgent
from multi_agent.pipeline import FixedPipeline


def test_multi_agent_m01_mixed_causes(session, ctx_a_operator):
    """IMP-201 (INVALID_DATE) 与 IMP-202 (UPSTREAM_TIMEOUT) 协同排查，产生两条独立证据链。"""
    coord = CoordinatorAgent(session, ctx_a_operator)
    res = coord.run("IMP-201 和 IMP-202 今天都失败了，看看是否同一原因")

    assert res["status"] in ("succeeded", "waiting_approval")
    facts_str = " ".join(res["facts"])
    # 必须分别包含两者的专属事实，不混为一谈
    assert "IMP-201" in facts_str
    assert "IMP-202" in facts_str
    assert "INVALID_DATE" in facts_str
    assert "UPSTREAM_TIMEOUT" in facts_str
    assert len(res["evidence_ids"]) >= 2


def test_multi_agent_m02_triggers_rework_loop(session, ctx_a_operator):
    """IMP-203 依赖幂等状态，Review 阶段识别到证据缺口，触发真实退回补查事件；因前置仍为 unknown，严格转人工而绝不放行重试。"""
    coord = CoordinatorAgent(session, ctx_a_operator)
    res = coord.run("IMP-203 超时了，请处理，需要的话申请重试")

    # 1. 检查 Tracer 中是否记录到了真实的 rework_requested 审查退回事件
    summary = coord.tracer.get_summary()
    rework_events = [e for e in summary["events"] if e["event_type"] == "rework_requested"]
    assert len(rework_events) >= 1, "必须触发由数据驱动的审查退回事件，证明非写死单向流水线"

    # 2. 检查 R02 核心整改：未知缺口不得被清空，终态必须为 needs_human，严禁生成重试提案
    assert res["status"] == "needs_human", "前置核查为 unknown 时必须升级人工介入，不得伪造通过"
    assert res["action_proposal"] is None, "前置条件未满足时严禁签发重试提案"
    assert "严禁盲目发起重试" in res["output"] or "升级" in res["output"]


def test_multi_agent_passed_prereq_allows_retry(session, ctx_a_operator):
    """IMP-202 前置幂等检查为 passed，允许复核通过并生成受控重试提案。"""
    coord = CoordinatorAgent(session, ctx_a_operator)
    res = coord.run("IMP-202 超时了，帮我处理并申请重试")

    # IMP-202 在种子数据中 idempotency_status 为 passed
    assert res["status"] in ("succeeded", "waiting_approval")
    assert res["action_proposal"] is not None
    assert res["action_proposal"]["action_type"] == "retry_import_job"



def test_multi_agent_cross_tenant_blocked(session, ctx_a_viewer):
    coord = CoordinatorAgent(session, ctx_a_viewer)
    res = coord.run("排查 IMP-B01 的故障原因")

    assert res["status"] == "needs_human_or_denied"
    assert "IMP-B01" not in res["output"] or "无权" in res["output"]


def test_fixed_pipeline_baseline(session, ctx_a_operator):
    """验证基线 B 固定流水线可正常运行，供 A/B/C 对照使用。"""
    pipe = FixedPipeline(session, ctx_a_operator)
    res = pipe.run("IMP-104 导入失败")

    assert res["status"] == "succeeded"
    assert any("IMP-104" in f for f in res["facts"])


def test_model_driven_coordinator_stub_loop(session, ctx_a_operator):
    """测试真实模型驱动的多智能体编排器（离线桩闭环模式）。"""
    from agent_core.model_client import OfflineStubModelClient
    from multi_agent.llm_coordinator import ModelDrivenCoordinatorAgent

    llm_coord = ModelDrivenCoordinatorAgent(
        session=session,
        ctx=ctx_a_operator,
        model_client=OfflineStubModelClient(),
    )
    res = llm_coord.run("IMP-104 导入失败，请协同排查")
    assert res["engine_mode"] == "llm"
    assert res["status"] in ("succeeded", "waiting_approval", "needs_human")
    assert len(res["evidence_ids"]) > 0


def test_model_driven_coordinator_reports_unavailable(session, ctx_a_operator):
    """未配置模型凭据时，模型驱动模式必须显式报错，严禁静默假装成功。"""
    from agent_core.model_client import ModelClient
    from multi_agent.llm_coordinator import ModelDrivenCoordinatorAgent

    llm_coord = ModelDrivenCoordinatorAgent(
        session=session,
        ctx=ctx_a_operator,
        model_client=ModelClient(base_url="", api_key="", model_name=""),
    )
    res = llm_coord.run("IMP-104 导入失败，请协同排查")
    assert res["status"] == "model_unavailable"
    assert "未配置" in res["output"] or "未就绪" in res["output"]

