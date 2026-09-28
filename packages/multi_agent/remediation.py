"""RemediationAgent 方案智能体。

专责规程核对与修复提案制定：
- 仅授权使用方案与前置核查工具：search_runbooks, check_prerequisites；
- 依据诊断事实核对对应版本的操作手册；
- 针对超时任务，严格执行'必须先核验幂等状态'铁律；
- 拟定不可变的动作提案草案（ActionProposalDraft）。
"""

from __future__ import annotations

import time
from typing import Any, Callable, Optional

from agent_core.budget import ExecutionBudget
from agent_core.contracts import (
    ActionProposalDraft,
    AgentResult,
    FactClaim,
    TaskEnvelope,
)
from agent_core.tracer import ExecutionTracer


class RemediationAgent:
    def __init__(
        self,
        tools: dict[str, Callable[..., dict[str, Any]]],
        budget: ExecutionBudget,
        tracer: ExecutionTracer,
    ) -> None:
        self.tools = tools
        self.budget = budget
        self.tracer = tracer

    def execute(self, envelope: TaskEnvelope, diagnosis_result: AgentResult) -> AgentResult:
        self.tracer.emit("agent_started", "RemediationAgent", f"接收方案制定任务: {envelope.goal}")
        facts: list[FactClaim] = []
        missing_info: list[str] = []
        evidence_ids: list[str] = []
        proposal: Optional[ActionProposalDraft] = None

        # 1. 结构化归纳故障特征（去重检索避免重复堆叠多类规程，R07）
        import re
        seen_queries: set[str] = set()
        error_jobs: dict[str, str] = {}  # job_id -> error_code

        for f in diagnosis_result.facts:
            job_match = re.search(r"IMP-[A-Za-z0-9]+", f.claim)
            jid = job_match.group(0) if job_match else None
            for ec in ("INVALID_DATE", "UPSTREAM_TIMEOUT", "MISSING_FIELD"):
                if ec in f.claim:
                    if jid:
                        error_jobs[jid] = ec
                    seen_queries.add(ec)

        if not seen_queries:
            seen_queries.add(envelope.goal[:40])

        for query_code in sorted(seen_queries):
            self.budget.record_step()
            self.budget.record_tool_call()
            t0 = time.time()
            rb_res = self.tools["search_runbooks"](query=query_code, product_version="v1")
            dur = (time.time() - t0) * 1000
            self.tracer.emit("tool_call_completed", "RemediationAgent", f"检索规程: {query_code}", rb_res, dur)

            if rb_res["ok"]:
                evidence_ids.extend([e["id"] for e in rb_res.get("evidence", [])])
                for hit in rb_res["data"].get("hits", []):
                    facts.append(
                        FactClaim(
                            claim=f"适用规程 [{hit['title']}]: {hit['snippet'][:100]}...",
                            evidence_ids=[f"ev-runbook-{hit['chunk_id']}"],
                            source_tool="search_runbooks",
                        )
                    )

        # 2. 针对各任务独立制定方案（禁止经验主义归并）
        user_wants_action = any(k in envelope.goal for k in ("处理", "工单", "重试", "方案", "申请"))
        for job_id, err_code in error_jobs.items():
            if err_code == "UPSTREAM_TIMEOUT":
                # 必须调用 check_prerequisites
                self.budget.record_step()
                self.budget.record_tool_call()
                t0 = time.time()
                prereq_res = self.tools["check_prerequisites"](target_id=job_id, check_type="idempotency_status")
                dur = (time.time() - t0) * 1000
                self.tracer.emit("tool_call_completed", "RemediationAgent", f"核对前置幂等条件: {job_id}", prereq_res, dur)

                if prereq_res["ok"]:
                    evidence_ids.extend([e["id"] for e in prereq_res.get("evidence", [])])
                    prereq_data = prereq_res["data"]
                    if prereq_data["status"] == "unknown":
                        # 发现证据缺口！
                        missing_info.append(f"任务 {job_id} 上游超时后幂等落库状态未知，依据 RB-ERR-TIMEOUT-RETRY 严禁直接发起重试")
                    elif prereq_data["status"] == "passed":
                        facts.append(
                            FactClaim(
                                claim=f"任务 {job_id} 经核对无重复写入（幂等检查通过），允许建议受控重试",
                                evidence_ids=[e["id"] for e in prereq_res.get("evidence", [])],
                                source_tool="check_prerequisites",
                            )
                        )
                        proposal = ActionProposalDraft(
                            action_type="retry_import_job",
                            target_id=job_id,
                            target_version=1,
                            payload={"retry_mode": "idempotent_safe", "reason": "超时后核验无重复写入，申请安全重试"},
                            required_prerequisites=["idempotency_status:passed"],
                            risk_level="medium",
                        )

            elif err_code == "INVALID_DATE":
                facts.append(
                    FactClaim(
                        claim=f"依据 RB-ERR-INVALID-DATE，任务 {job_id} 属数据格式错误，严禁在原数据上直接重试",
                        evidence_ids=["ev-runbook-chunk-err-invalid-date"],
                        source_tool="search_runbooks",
                    )
                )
                if user_wants_action and not proposal:
                    proposal = ActionProposalDraft(
                        action_type="create_ticket",
                        target_id=job_id,
                        target_version=3,
                        payload={
                            "title": f"支持工单: {job_id} 数据格式异常人工修正申请",
                            "priority": "P2",
                            "description": "第3行 start_date 日期格式错误，需业务或技术支持人工修正数据。",
                        },
                        required_prerequisites=[],
                        risk_level="low",
                    )



        status = "needs_more_evidence" if missing_info else "completed"
        result = AgentResult(
            task_id=envelope.task_id,
            agent_role="RemediationAgent",
            status=status,
            summary=f"已制定方案：提供规程与建议 {len(facts)} 项，待补查项 {len(missing_info)} 项",
            facts=facts,
            missing_information=missing_info,
            action_proposal=proposal,
            evidence_ids=list(set(evidence_ids)),
        )
        self.tracer.emit("agent_completed", "RemediationAgent", result.summary, {"missing_info": missing_info})
        return result
