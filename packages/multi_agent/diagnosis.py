"""DiagnosisAgent 诊断智能体。

专责技术事实调查：
- 仅授权使用技术只读工具：get_import_job, get_job_logs, validate_csv；
- 独立调查传入 scope 中的每个任务；
- 输出严格挂载 evidence ID 的事实列表 FactClaim，不擅自推断规程或写操作。
"""

from __future__ import annotations

import time
from typing import Any, Callable

from agent_core.budget import ExecutionBudget
from agent_core.contracts import AgentResult, FactClaim, Hypothesis, TaskEnvelope
from agent_core.tracer import ExecutionTracer


class DiagnosisAgent:
    def __init__(
        self,
        tools: dict[str, Callable[..., dict[str, Any]]],
        budget: ExecutionBudget,
        tracer: ExecutionTracer,
    ) -> None:
        self.tools = tools
        self.budget = budget
        self.tracer = tracer

    def execute(self, envelope: TaskEnvelope) -> AgentResult:
        self.tracer.emit("agent_started", "DiagnosisAgent", f"接收子任务: {envelope.goal}", envelope.scope)
        job_ids = envelope.scope.get("job_ids", [])
        facts: list[FactClaim] = []
        hypotheses: list[Hypothesis] = []
        missing_info: list[str] = []
        evidence_ids: list[str] = []

        for job_id in job_ids:
            # 1. 查任务状态
            self.budget.record_step()
            self.budget.record_tool_call()
            t0 = time.time()
            job_res = self.tools["get_import_job"](job_id=job_id)
            dur = (time.time() - t0) * 1000
            self.tracer.emit("tool_call_completed", "DiagnosisAgent", f"查任务状态 {job_id}", job_res, dur)

            if not job_res["ok"]:
                missing_info.append(f"任务 {job_id} 查询失败: {job_res['error']['message']}")
                continue

            jdata = job_res["data"]
            j_ev = [e["id"] for e in job_res.get("evidence", [])]
            evidence_ids.extend(j_ev)

            err_code = jdata.get("error_code")
            file_id = jdata.get("file_id")
            facts.append(
                FactClaim(
                    claim=f"任务 {job_id} 状态为 failed，错误码为 {err_code}",
                    evidence_ids=j_ev,
                    source_tool="get_import_job",
                )
            )

            # 2. 查日志
            self.budget.record_step()
            self.budget.record_tool_call()
            t0 = time.time()
            log_res = self.tools["get_job_logs"](job_id=job_id)
            dur = (time.time() - t0) * 1000
            self.tracer.emit("tool_call_completed", "DiagnosisAgent", f"查任务日志 {job_id}", log_res, dur)

            if not log_res["ok"]:
                # 记录超时或失败，不伪造日志
                missing_info.append(f"任务 {job_id} 日志未能取得（{log_res['error']['message']}）")
                hypotheses.append(Hypothesis(claim=f"任务 {job_id} 可能因上游暂时性超时中断", status="unconfirmed"))
            else:
                log_ev = [e["id"] for e in log_res.get("evidence", [])]
                evidence_ids.extend(log_ev)
                for log_item in log_res["data"].get("logs", []):
                    if log_item["level"] == "ERROR":
                        facts.append(
                            FactClaim(
                                claim=f"任务 {job_id} 日志报错: {log_item['message']}",
                                evidence_ids=[f"ev-log-{log_item['log_id']}"],
                                source_tool="get_job_logs",
                            )
                        )

            # 3. 若为文件数据错误，查 CSV 校验
            if file_id and err_code in ("INVALID_DATE", "MISSING_FIELD"):
                self.budget.record_step()
                self.budget.record_tool_call()
                t0 = time.time()
                csv_res = self.tools["validate_csv"](file_id=file_id)
                dur = (time.time() - t0) * 1000
                self.tracer.emit("tool_call_completed", "DiagnosisAgent", f"校验文件 {file_id}", csv_res, dur)
                if csv_res["ok"]:
                    csv_ev = [e["id"] for e in csv_res.get("evidence", [])]
                    evidence_ids.extend(csv_ev)
                    for err in csv_res["data"].get("errors", []):
                        facts.append(
                            FactClaim(
                                claim=f"任务 {job_id} 文件第 {err['row']} 行字段 {err['field']} 错误: {err['message']}",
                                evidence_ids=csv_ev,
                                source_tool="validate_csv",
                            )
                        )

        status = "completed" if facts else "needs_more_evidence"
        result = AgentResult(
            task_id=envelope.task_id,
            agent_role="DiagnosisAgent",
            status=status,
            summary=f"已对 {len(job_ids)} 个任务完成技术调查，采集事实 {len(facts)} 条",
            facts=facts,
            hypotheses=hypotheses,
            missing_information=missing_info,
            evidence_ids=list(set(evidence_ids)),
        )
        self.tracer.emit("agent_completed", "DiagnosisAgent", result.summary, {"facts_count": len(facts)})
        return result
