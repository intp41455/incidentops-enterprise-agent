"""ReviewAgent 审查智能体（质检与复核核心）。

专责证据链与规程复核：
1. 检查关键主张是否具备真实 evidence ID（无证据主张直接驳回）；
2. 检查多任务排查是否符合'独立证据、禁止强行归并'（RB-INCIDENT-TRIAGE）；
3. 检查超时任务重试是否满足'必须先核实幂等状态'（RB-ERR-TIMEOUT-RETRY）；
4. 审查不通过时，触发退回补查（REWORK，上限2轮）；超限或冲突严重时转人工（ESCALATE）。
"""

from __future__ import annotations

from typing import Optional

from agent_core.budget import ExecutionBudget
from agent_core.contracts import AgentResult, ReviewVerdict
from agent_core.tracer import ExecutionTracer


class ReviewAgent:
    def __init__(self, budget: ExecutionBudget, tracer: ExecutionTracer) -> None:
        self.budget = budget
        self.tracer = tracer

    def review(
        self,
        diagnosis_result: AgentResult,
        remediation_result: AgentResult,
        user_intent: str = "",
    ) -> ReviewVerdict:
        self.tracer.emit("agent_started", "ReviewAgent", "开始独立复核证据链与操作方案")

        missing_evidence: list[str] = []
        contradictions: list[str] = []

        # 1. 证据真实挂载检查
        all_facts = diagnosis_result.facts + remediation_result.facts
        for f in all_facts:
            if not f.evidence_ids:
                missing_evidence.append(f"主张 [{f.claim}] 缺少证据 ID 关联")

        # 2. 检查多任务经验主义归并（若多任务错误码不同，严禁断言为同一根因）
        err_codes: set[str] = set()
        for f in diagnosis_result.facts:
            if "INVALID_DATE" in f.claim:
                err_codes.add("INVALID_DATE")
            elif "UPSTREAM_TIMEOUT" in f.claim:
                err_codes.add("UPSTREAM_TIMEOUT")

        if len(err_codes) > 1:
            # 存在混合故障
            # 检查是否有未证假设误将二者合并
            for h in diagnosis_result.hypotheses:
                if "同一原因" in h.claim and h.status == "confirmed":
                    contradictions.append("违反 RB-INCIDENT-TRIAGE：不同任务错误码不同，禁止合并为单一原因")

        # 3. 检查重试前置条件与证据缺口
        if remediation_result.missing_information:
            for gap in remediation_result.missing_information:
                if "幂等落库状态未知" in gap:
                    missing_evidence.append(gap)

        # 检查是否对 INVALID_DATE 任务提出了重试
        if remediation_result.action_proposal:
            prop = remediation_result.action_proposal
            if prop.action_type == "retry_import_job":
                # 检查该提案的具体目标任务是否属于数据格式错误
                if any(f"任务 {prop.target_id}" in f.claim and "INVALID_DATE" in f.claim for f in diagnosis_result.facts):
                    contradictions.append(f"违反 RB-ERR-INVALID-DATE：任务 {prop.target_id} 属数据格式错误，严禁直接重试")


        # 4. 裁决决策
        if missing_evidence or contradictions:
            # 判断是否允许退回补查
            can_rework = self.budget.record_rework_round()
            if can_rework:
                rework_reason = f"证据链复核未通过，退回补查（第 {self.budget.current_rework_rounds} 轮）。原因: {'; '.join(missing_evidence + contradictions)}"
                self.tracer.emit(
                    "rework_requested",
                    "ReviewAgent",
                    rework_reason,
                    {"missing_evidence": missing_evidence, "contradictions": contradictions},
                )
                return ReviewVerdict(
                    decision="REWORK",
                    reason=rework_reason,
                    rework_target_agent="remediation",
                    missing_evidence=missing_evidence,
                    contradictions=contradictions,
                    approved_proposal=None,
                )
            else:
                escalate_reason = f"已达最大退回补查轮次上限 ({self.budget.max_rework_rounds})，仍存在未消解的证据缺口或矛盾，转人工介入 (needs_human)"
                self.tracer.emit("review_completed", "ReviewAgent", escalate_reason, {"decision": "ESCALATE"})
                return ReviewVerdict(
                    decision="ESCALATE",
                    reason=escalate_reason,
                    missing_evidence=missing_evidence,
                    contradictions=contradictions,
                    approved_proposal=None,
                )

        # 复核全部通过
        pass_reason = "复核通过：所有关键事实均有证据 ID 支撑，操作方案符合运维规范与前置条件"
        self.tracer.emit(
            "review_completed",
            "ReviewAgent",
            pass_reason,
            {"decision": "PASS", "proposal": remediation_result.action_proposal is not None},
        )
        return ReviewVerdict(
            decision="PASS",
            reason=pass_reason,
            approved_proposal=remediation_result.action_proposal,
        )
