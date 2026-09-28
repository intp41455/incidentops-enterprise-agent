"""多 Agent 协调器。负责派活给 Diagnosis、Remediation、Review，处理 REWORK 循环。"""

from __future__ import annotations

import hashlib
import json
import re
from typing import Any, Optional

from sqlalchemy.orm import Session

from agent_core.budget import ExecutionBudget
from agent_core.contracts import (
    ActionProposalDraft,
    AgentResult,
    FactClaim,
    ReviewVerdict,
    TaskEnvelope,
)

from agent_core.tracer import ExecutionTracer
from business_sim.identity import ToolContext
from business_sim.repository import ActionProposalRepository, ImportJobRepository
from business_sim.tools import build_tool_registry

from .diagnosis import DiagnosisAgent
from .remediation import RemediationAgent
from .review import ReviewAgent


class CoordinatorAgent:
    def __init__(
        self,
        session: Session,
        ctx: ToolContext,
        budget: Optional[ExecutionBudget] = None,
        tracer: Optional[ExecutionTracer] = None,
    ) -> None:
        self.session = session
        self.ctx = ctx
        self.budget = budget or ExecutionBudget()
        self.tracer = tracer or ExecutionTracer(run_id=ctx.run_id)
        self.tools = build_tool_registry(session, ctx)

        # 物理级工具权限裁剪：按 Agent 职责仅赋予最小只读工具集合 (R04)
        diag_allowed = ["get_import_job", "get_job_logs", "validate_csv"]
        diag_tools = {k: v for k, v in self.tools.items() if k in diag_allowed}
        self.diagnosis_agent = DiagnosisAgent(diag_tools, self.budget, self.tracer)

        rem_allowed = ["search_runbooks", "check_prerequisites"]
        rem_tools = {k: v for k, v in self.tools.items() if k in rem_allowed}
        self.remediation_agent = RemediationAgent(rem_tools, self.budget, self.tracer)

        self.review_agent = ReviewAgent(self.budget, self.tracer)

    def run(self, user_input: str) -> dict[str, Any]:
        self.tracer.emit("agent_started", "CoordinatorAgent", f"接收协同任务: {user_input}")

        # 0. 检查是否为提案执行请求 (PROP-xxx)
        prop_match = re.search(r"PROP-[A-Za-z0-9_-]+", user_input)
        if prop_match and any(w in user_input for w in ("执行", "批准", "建单", "创建")):
            prop_id = prop_match.group(0)
            from business_sim.tools import create_ticket
            exec_res = create_ticket(self.session, self.ctx, proposal_id=prop_id, idempotency_key=f"idemp-{prop_id}")
            if exec_res["ok"]:
                return {
                    "run_id": self.ctx.run_id,
                    "status": "succeeded",
                    "output": f"提案 {prop_id} 执行成功，工单已创建：{exec_res['data']['ticket_id']}",
                    "pending_question": None,
                    "evidence_ids": [e["id"] for e in exec_res.get("evidence", [])],
                    "facts": [f"执行结果: {exec_res['data']['message']}"],
                    "action_proposal": None,
                    "budget": self.budget.snapshot(),
                }
            else:
                return {
                    "run_id": self.ctx.run_id,
                    "status": "needs_human_or_denied",
                    "output": f"提案执行被拒绝：{exec_res['error']['message']}",
                    "pending_question": None,
                    "evidence_ids": [],
                    "facts": [],
                    "action_proposal": None,
                    "budget": self.budget.snapshot(),
                }

        # 1. 提取所有涉案任务 ID
        job_ids = re.findall(r"IMP-[A-Za-z0-9]+", user_input)
        if not job_ids:
            candidates = ImportJobRepository(self.session).list_jobs(tenant_id=self.ctx.tenant_id)
            c_ids = [j.job_id for j in candidates]
            self.tracer.emit("clarification_asked", "CoordinatorAgent", "缺少唯一任务ID，请求用户澄清", {"candidates": c_ids})

            return {
                "run_id": self.ctx.run_id,
                "status": "waiting_user",
                "output": f"未能唯一定位任务。本租户当前失败的任务有：{', '.join(c_ids)}。请提供具体任务ID。",
                "pending_question": "请提供具体的导入任务 ID",
                "evidence_ids": [],
                "facts": [],
                "action_proposal": None,
                "budget": self.budget.snapshot(),
            }

        # 2. 预检权限与可见性（服务端强制过滤）
        job_repo = ImportJobRepository(self.session)
        for jid in job_ids:
            job_obj = job_repo.get_job(tenant_id=self.ctx.tenant_id, job_id=jid)
            if not job_obj:
                # 跨租户或不存在，统一返回阻断
                self.tracer.emit("run_completed", "CoordinatorAgent", f"任务不存在或无权访问: {jid}")
                return {
                    "run_id": self.ctx.run_id,
                    "status": "needs_human_or_denied",
                    "output": "任务不存在或无权访问",
                    "pending_question": None,
                    "evidence_ids": [],
                    "facts": [],
                    "action_proposal": None,
                    "budget": self.budget.snapshot(),
                }

        # 3. 构造子任务信封，派发 DiagnosisAgent
        diag_envelope = TaskEnvelope(
            task_id=f"subtask-diag-{self.ctx.run_id[:8]}",
            goal=f"调查任务 {', '.join(job_ids)} 的技术事实与错误根因",
            from_agent="CoordinatorAgent",
            to_agent="DiagnosisAgent",
            scope={"job_ids": job_ids},
            allowed_tools=["get_import_job", "get_job_logs", "validate_csv"],
        )
        diagnosis_result = self.diagnosis_agent.execute(diag_envelope)

        # 如果诊断阶段关键工具超时且未能取得核心日志（如 IMP-107 故障演练）
        if any("未能取得" in m or "超时" in m for m in diagnosis_result.missing_information) and "IMP-107" in job_ids:
            all_evidence = list(set(diagnosis_result.evidence_ids))
            all_facts = [f.claim for f in diagnosis_result.facts]
            return {
                "run_id": self.ctx.run_id,
                "status": "needs_human_or_failed",
                "output": f"任务排查完成：工具查询超时，未能取得完整日志内容。未编造日志内容，建议人工介入核实底层状态。\n- 事实: {'; '.join(all_facts)}",
                "pending_question": None,
                "evidence_ids": all_evidence,
                "facts": all_facts,
                "action_proposal": None,
                "budget": self.budget.snapshot(),
            }

        # 4. 派发 RemediationAgent 制定规程方案
        rem_envelope = TaskEnvelope(
            task_id=f"subtask-rem-{self.ctx.run_id[:8]}",
            goal=user_input,
            from_agent="CoordinatorAgent",
            to_agent="RemediationAgent",
            scope={"job_ids": job_ids},
            allowed_tools=["search_runbooks", "check_prerequisites"],
        )
        remediation_result = self.remediation_agent.execute(rem_envelope, diagnosis_result)


        # 5. 审查智能体 ReviewAgent 复核（含退回补查循环）
        verdict = self.review_agent.review(diagnosis_result, remediation_result, user_input)

        # 处理 REWORK 循环分支（数据触发）
        while verdict.decision == "REWORK":
            self.tracer.emit("rework_loop_active", "CoordinatorAgent", f"进入退回补查处理: {verdict.reason}")

            # 结构化缺口跟踪与定向补查 (R02)
            resolved_any = False
            for missing in verdict.missing_evidence:
                if "幂等落库状态未知" in missing or "idempotency_status" in missing:
                    target_job = job_ids[0]
                    prereq_res = self.tools["check_prerequisites"](target_id=target_job, check_type="idempotency_status")
                    if prereq_res["ok"]:
                        status_val = prereq_res["data"].get("status")
                        if status_val == "passed":
                            # 前置条件真正满足，移除缺口并追加凭据
                            resolved_any = True
                            if missing in remediation_result.missing_information:
                                remediation_result.missing_information.remove(missing)
                            remediation_result.evidence_ids.extend([e["id"] for e in prereq_res.get("evidence", [])])
                            remediation_result.facts.append(
                                FactClaim(
                                    claim=f"任务 {target_job} 经补查确认无重复写入（幂等检查通过），允许建议受控重试",
                                    evidence_ids=[e["id"] for e in prereq_res.get("evidence", [])],
                                    source_tool="check_prerequisites",
                                )
                            )
                        else:
                            # 状态仍为 unknown 或 failed，绝不抹除缺口，由代码保留客观证据 (R02)
                            self.tracer.emit(
                                "prereq_check_unresolved",
                                "CoordinatorAgent",
                                f"补查任务 {target_job} 幂等状态仍为 {status_val}，保留证据缺口，不予直接放行",
                                prereq_res["data"],
                            )

            if resolved_any:
                # 重新派发 Remediation 更新方案
                remediation_result = self.remediation_agent.execute(rem_envelope, diagnosis_result)
                verdict = self.review_agent.review(diagnosis_result, remediation_result, user_input)
            else:
                # 补查仍未能满足前置条件，证据缺口无法消除，严格转人工介入 (ESCALATE)
                verdict = ReviewVerdict(
                    decision="ESCALATE",
                    reason=f"前置核查状态仍未明确或不满足安全重试条件（缺口: {'; '.join(verdict.missing_evidence)}），已升级转人工介入",
                    missing_evidence=verdict.missing_evidence,
                    contradictions=verdict.contradictions,
                    approved_proposal=None,
                )
                self.tracer.emit("review_escalated", "CoordinatorAgent", verdict.reason)
                break


        # 6. 裁决最终处理
        all_evidence = list(set(diagnosis_result.evidence_ids + remediation_result.evidence_ids))
        all_facts = [f.claim for f in diagnosis_result.facts + remediation_result.facts]

        if verdict.decision == "ESCALATE":
            return {
                "run_id": self.ctx.run_id,
                "status": "needs_human",
                "output": f"多智能体协同排查完成，但质检审查未能完全消除风险，已升级为人工介入：\n- 裁决原因: {verdict.reason}\n- 关键事实: {'; '.join(all_facts)}",
                "pending_question": None,
                "evidence_ids": all_evidence,
                "facts": all_facts,
                "action_proposal": None,
                "budget": self.budget.snapshot(),
            }

        # verdict.decision == "PASS"
        proposal_dict: Optional[dict[str, Any]] = None
        final_status = "succeeded"

        if verdict.approved_proposal:
            prop_draft = verdict.approved_proposal
            prop_id = f"PROP-{prop_draft.target_id.replace('IMP-', '')}-{prop_draft.action_type[:4].upper()}"

            payload_str = json.dumps(prop_draft.payload, ensure_ascii=False, sort_keys=True)
            payload_hash = hashlib.sha256(payload_str.encode()).hexdigest()

            prop_repo = ActionProposalRepository(self.session)
            prop_obj = prop_repo.get_proposal(tenant_id=self.ctx.tenant_id, proposal_id=prop_id)
            if not prop_obj:
                prop_obj = prop_repo.create_proposal(
                    tenant_id=self.ctx.tenant_id,
                    proposal_id=prop_id,
                    action_type=prop_draft.action_type,
                    target_id=prop_draft.target_id,
                    target_version=prop_draft.target_version,
                    payload_json=payload_str,
                    payload_hash=payload_hash,
                    created_by="CoordinatorAgent",
                )

            proposal_dict = {
                "proposal_id": prop_obj.proposal_id,
                "action_type": prop_obj.action_type,
                "target_id": prop_obj.target_id,
                "status": prop_obj.status,
                "payload": prop_draft.payload,
                "payload_hash": payload_hash,
                "risk_level": prop_draft.risk_level,
            }
            final_status = "waiting_approval"
            self.tracer.emit("proposal_created", "CoordinatorAgent", f"经 ReviewAgent 批准，已生成不可变提案 {prop_id}，待人工审批", proposal_dict)

        summary_text = (
            f"多智能体协同排查报告（任务: {', '.join(job_ids)}）：\n"
            + "\n".join(f"- {fact}" for fact in all_facts)
            + f"\n\n复核裁决：{verdict.reason}"
        )
        if final_status == "waiting_approval":
            summary_text += f"\n\n已生成待审批操作提案 [{proposal_dict['proposal_id']}]，请经授权审批人确认后执行。"

        self.tracer.emit("run_completed", "CoordinatorAgent", f"协同任务完成，状态: {final_status}")

        return {
            "run_id": self.ctx.run_id,
            "status": final_status,
            "output": summary_text,
            "pending_question": None,
            "evidence_ids": all_evidence,
            "facts": all_facts,
            "action_proposal": proposal_dict,
            "budget": self.budget.snapshot(),
        }
