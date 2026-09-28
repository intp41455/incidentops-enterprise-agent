"""agent_core 基础设施单元测试。

原则：不联网。真实模型调用只在 `scripts/d2_real_run.py` 里手工执行并留痕；
此处用显式的 `OfflineStubModelClient` 验证"模型决策 → 工具执行 → 观察回灌"这条链路本身。
"""

import pytest
from agent_core.budget import BudgetExceededException, ExecutionBudget
from agent_core.contracts import AgentResult, FactClaim, TaskEnvelope
from agent_core.model_agent import ModelDrivenAgent
from agent_core.model_client import (
    ModelClient,
    ModelUnavailableError,
    OfflineStubModelClient,
)
from agent_core.tracer import ExecutionTracer


def test_budget_exceeded():
    budget = ExecutionBudget(max_steps=2)
    budget.record_step()
    budget.record_step()
    with pytest.raises(BudgetExceededException):
        budget.record_step()


def test_budget_rework_rounds():
    budget = ExecutionBudget(max_rework_rounds=2)
    assert budget.record_rework_round() is True
    assert budget.record_rework_round() is True
    assert budget.record_rework_round() is False


def test_tracer_records_events():
    tracer = ExecutionTracer(run_id="run-test-01")
    tracer.emit("step_start", "coordinator", "开始执行任务")
    tracer.emit("tool_call_completed", "diagnosis", "调用 get_import_job 成功", {"job_id": "IMP-104"})

    summary = tracer.get_summary()
    assert summary["total_events"] == 2
    assert summary["tool_calls_count"] == 1


def test_model_client_without_key_raises_instead_of_faking():
    """没有 Key 必须显式失败，绝不返回编造的工具调用与用量。"""
    client = ModelClient(base_url="", api_key="", model_name="")
    with pytest.raises(ModelUnavailableError):
        client.chat_completion(messages=[{"role": "user", "content": "查 IMP-104"}])


def test_offline_stub_is_marked_and_not_billable():
    """离线桩必须自我标识，且不产生任何真实 token 与费用。"""
    client = OfflineStubModelClient()
    resp = client.chat_completion(messages=[{"role": "user", "content": "请查 IMP-104 为什么失败"}])

    assert resp["usage_source"] == "offline-stub"
    assert resp["prompt_tokens"] == 0 and resp["completion_tokens"] == 0
    assert resp["cost_cny"] == 0.0
    assert resp["tool_calls"][0]["name"] == "get_import_job"
    assert resp["tool_calls"][0]["arguments"]["job_id"] == "IMP-104"


def test_model_driven_agent_loop_hits_real_tools(session, ctx_a_viewer):
    """模型侧给出 tool_calls 后，循环必须真的执行工具并把真实证据带回。"""
    agent = ModelDrivenAgent(
        session=session,
        ctx=ctx_a_viewer,
        model_client=OfflineStubModelClient(),
        tracer=ExecutionTracer(run_id=ctx_a_viewer.run_id),
    )
    result = agent.run("请查一下导入任务 IMP-104 为什么失败")

    assert result["status"] == "answered"
    assert result["tool_calls"] == ["get_import_job"]
    assert "ev-job-IMP-104-v3" in result["evidence_ids"]
    assert result["usage_source"] == ["offline-stub"]
    assert result["prompt_tokens"] == 0 and result["cost_cny"] == 0.0


def test_model_driven_agent_reports_model_unavailable(session, ctx_a_viewer):
    """模型不可用时状态必须明示，不得给出任何诊断结论。"""
    agent = ModelDrivenAgent(
        session=session,
        ctx=ctx_a_viewer,
        model_client=ModelClient(base_url="", api_key="", model_name=""),
        tracer=ExecutionTracer(run_id=ctx_a_viewer.run_id),
    )
    result = agent.run("请查一下导入任务 IMP-104 为什么失败")

    assert result["status"] == "model_unavailable"
    assert result["tool_calls"] == []
    assert result["evidence_ids"] == []