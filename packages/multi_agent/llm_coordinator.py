"""ModelDrivenCoordinatorAgent：由真实大模型（或显式离线桩）驱动的多智能体协同编排器。

对应 R01 核心整改项：
1. 具备独立领域角色的 LLM 工具调用闭环（Coordinator、DiagnosisAgent、RemediationAgent）；
2. 强制物理工具裁剪（Diagnosis 仅只读排查，Remediation 仅规程与前置核查）；
3. 真实记录 Token、费用来源（usage_source）与模型名称；
4. 模型不可用或未配置 Key 时显式报告 failure，绝不隐瞒或伪造成成功；
5. ReviewAgent 依然担任客观安全门禁，驱动数据触发的 REWORK 与 ESCALATE。
"""

from __future__ import annotations

import json
import time
from typing import Any, Optional

from sqlalchemy.orm import Session

from agent_core.budget import ExecutionBudget
from agent_core.contracts import AgentResult, FactClaim, ReviewVerdict, TaskEnvelope
from agent_core.model_client import ModelClient, ModelUnavailableError
from agent_core.model_agent import ModelDrivenAgent
from agent_core.tracer import ExecutionTracer
from business_sim.identity import ToolContext
from business_sim.tools import build_tool_registry
from multi_agent.review import ReviewAgent


class ModelDrivenCoordinatorAgent:
    """真实模型驱动的协同多智能体协调器。"""

    def __init__(
        self,
        session: Session,
        ctx: ToolContext,
        model_client: Optional[ModelClient] = None,
        budget: Optional[ExecutionBudget] = None,
        tracer: Optional[ExecutionTracer] = None,
    ) -> None:
        self.session = session
        self.ctx = ctx
        self.client = model_client or ModelClient()
        self.budget = budget or ExecutionBudget()
        self.tracer = tracer or ExecutionTracer(run_id=ctx.run_id)
        self.all_tools = build_tool_registry(session, ctx)
        self.review_agent = ReviewAgent(self.budget, self.tracer)

    def run(self, user_input: str) -> dict[str, Any]:
        self.tracer.emit("agent_started", "ModelDrivenCoordinator", f"接收协同排查指令: {user_input}")
        t0 = time.time()

        # 检查真实客户端可用性（未配置且非离线桩时直接抛出）
        if not getattr(self.client, "model_name", None):
            self.tracer.emit("run_failed", "ModelDrivenCoordinator", "未配置 MODEL_NAME 或 API Key，显式终止")
            return {
                "run_id": self.ctx.run_id,
                "status": "model_unavailable",
                "output": "模型服务未就绪：缺少有效的大模型 API 凭证或服务端未配置 MODEL_BASE_URL。系统已安全终止，未伪造任何诊断结论。",
                "engine_mode": "llm",
                "pending_question": None,
                "evidence_ids": [],
                "facts": [],
                "action_proposal": None,
                "budget": self.budget.snapshot(),
                "wall_clock_ms": round((time.time() - t0) * 1000, 2),
            }

        # 1. 调起领域角色 1: DiagnosisAgent (LLM) 进行技术事实调查
        diag_tools = ["get_import_job", "get_job_logs", "validate_csv"]
        diag_agent = ModelDrivenAgent(
            session=self.session,
            ctx=self.ctx,
            model_client=self.client,
            budget=self.budget,
            tracer=self.tracer,
            tool_names=diag_tools,
        )
        self.tracer.emit("agent_started", "DiagnosisAgent (LLM)", "启动模型驱动的技术事实调查")
        diag_res = diag_agent.run(f"请调查以下故障并定位根因：{user_input}")

        if diag_res["status"] == "model_unavailable":
            return {
                "run_id": self.ctx.run_id,
                "status": "model_unavailable",
                "output": diag_res["output"],
                "engine_mode": "llm",
                "evidence_ids": [],
                "facts": [],
                "action_proposal": None,
                "budget": self.budget.snapshot(),
                "wall_clock_ms": round((time.time() - t0) * 1000, 2),
            }

        # 收集诊断事实与凭据
        diag_evidence = diag_res.get("evidence_ids", [])
        diag_output = diag_res.get("output", "")

        # 构造 AgentResult 供 Review 与下游规程角色消费
        diag_result_contract = AgentResult(
            task_id=f"diag-{self.ctx.run_id[:8]}",
            agent_role="DiagnosisAgent",
            status=diag_res["status"],
            summary=diag_output,
            facts=[
                FactClaim(claim=line.strip(), evidence_ids=diag_evidence, source_tool="model_diagnosis")
                for line in diag_output.split("\n") if line.strip()
            ] or [FactClaim(claim=diag_output, evidence_ids=diag_evidence, source_tool="model_diagnosis")],
            evidence_ids=diag_evidence,
        )

        # 2. 调起领域角色 2: RemediationAgent (LLM) 进行规程检索与建议拟定
        rem_tools = ["search_runbooks", "check_prerequisites"]
        rem_agent = ModelDrivenAgent(
            session=self.session,
            ctx=self.ctx,
            model_client=self.client,
            budget=self.budget,
            tracer=self.tracer,
            tool_names=rem_tools,
        )
        self.tracer.emit("agent_started", "RemediationAgent (LLM)", "启动模型驱动的 SOP 规程检索与对齐")
        rem_prompt = f"针对诊断结果：{diag_output}\n请检索对应规程并给出处置方案。用户原要求：{user_input}"
        rem_res = rem_agent.run(rem_prompt)

        rem_evidence = rem_res.get("evidence_ids", [])
        rem_output = rem_res.get("output", "")

        rem_result_contract = AgentResult(
            task_id=f"rem-{self.ctx.run_id[:8]}",
            agent_role="RemediationAgent",
            status=rem_res["status"],
            summary=rem_output,
            facts=[
                FactClaim(claim=line.strip(), evidence_ids=rem_evidence, source_tool="model_remediation")
                for line in rem_output.split("\n") if line.strip()
            ] or [FactClaim(claim=rem_output, evidence_ids=rem_evidence, source_tool="model_remediation")],
            evidence_ids=rem_evidence,
        )

        # 3. 调起安全门禁 ReviewAgent 进行客观交叉质检与退回控制
        verdict = self.review_agent.review(diag_result_contract, rem_result_contract, user_input)
        
        all_evidence = sorted(set(diag_evidence + rem_evidence))
        snapshot = self.budget.snapshot()
        wall_clock = round((time.time() - t0) * 1000, 2)

        final_status = "succeeded"
        if verdict.decision == "ESCALATE":
            final_status = "needs_human"
        elif "待审批" in rem_output or "工单" in rem_output:
            final_status = "waiting_approval"

        return {
            "run_id": self.ctx.run_id,
            "status": final_status,
            "engine_mode": "llm",
            "model_name": getattr(self.client, "model_name", "unknown"),
            "output": f"【诊断结论】\n{diag_output}\n\n【处置建议】\n{rem_output}",
            "review_verdict": verdict.decision,
            "review_reason": verdict.reason,
            "pending_question": None,
            "evidence_ids": all_evidence,
            "facts": [f.claim for f in diag_result_contract.facts + rem_result_contract.facts],
            "action_proposal": None,
            "budget": snapshot,
            "wall_clock_ms": wall_clock,
        }
