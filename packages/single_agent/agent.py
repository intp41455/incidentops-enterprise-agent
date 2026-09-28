"""单 Agent 基线（确定性脚本）。按固定流程查任务、查日志、找规程、生成提案。"""

from __future__ import annotations

import hashlib
import json
import re
import time
from typing import Any, Optional

from sqlalchemy.orm import Session

from agent_core.budget import ExecutionBudget
from agent_core.model_client import ModelClient
from agent_core.tracer import ExecutionTracer
from business_sim.identity import ToolContext
from business_sim.repository import ActionProposalRepository, ImportJobRepository
from business_sim.tools import (
    TOOL_GET_IMPORT_JOB,
    TOOL_GET_JOB_LOGS,
    TOOL_SCHEMAS,
    build_tool_registry,
)


class OpsDeskAgent:
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
        self.registry = build_tool_registry(session, ctx)

    def run(self, user_input: str) -> dict[str, Any]:
        """运行单 Agent 任务主循环。"""
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

        # 1. 检查是否存在唯一定位的任务 ID
        job_match = re.search(r"IMP-[A-Za-z0-9]+", user_input)

        if not job_match:
            # 候选任务查询（仅限当前租户）
            candidates = ImportJobRepository(self.session).list_jobs(tenant_id=self.ctx.tenant_id)
            c_ids = [j.job_id for j in candidates]
            self.tracer.emit("clarification_asked", "OpsDeskAgent", "缺少唯一任务ID，请求用户澄清", {"candidates": c_ids})
            return {
                "run_id": self.ctx.run_id,
                "status": "waiting_user",
                "output": f"未能唯一定位任务。本租户当前失败的任务有：{', '.join(c_ids)}。请提供具体任务ID（如 {c_ids[0] if c_ids else 'IMP-104'}）。",
                "pending_question": "请提供具体的导入任务 ID",
                "evidence_ids": [],
                "facts": [],
                "action_proposal": None,
                "budget": self.budget.snapshot(),
            }

        target_job_id = job_match.group(0)

        collected_evidence_ids: list[str] = []
        collected_facts: list[str] = []
        retry_counts: dict[str, int] = {}
        proposal_record: Optional[dict[str, Any]] = None

        # 2. 工具调用循环
        # 第一步：查询导入任务
        self.budget.record_step()
        self.budget.record_tool_call()
        t0 = time.time()
        job_res = self.registry[TOOL_GET_IMPORT_JOB](job_id=target_job_id)
        dur = (time.time() - t0) * 1000
        self.tracer.emit("tool_call_completed", "OpsDeskAgent", f"查询任务 {target_job_id}", job_res, dur)

        if not job_res["ok"]:
            # 跨租户或不存在
            err_msg = job_res["error"]["message"]
            self.tracer.emit("run_completed", "OpsDeskAgent", f"任务被拒绝或不存在: {err_msg}")
            return {
                "run_id": self.ctx.run_id,
                "status": "needs_human_or_denied",
                "output": f"查询任务失败：{err_msg}",
                "pending_question": None,
                "evidence_ids": [],
                "facts": [],
                "action_proposal": None,
                "budget": self.budget.snapshot(),
            }

        job_data = job_res["data"]
        collected_evidence_ids.extend([e["id"] for e in job_res.get("evidence", [])])
        file_id = job_data.get("file_id")
        error_code = job_data.get("error_code")
        collected_facts.append(f"任务 {target_job_id} 状态为 failed，错误码为 {error_code}")

        # 第二步：查询日志
        self.budget.record_step()
        self.budget.record_tool_call()
        t0 = time.time()
        log_res = self.registry[TOOL_GET_JOB_LOGS](job_id=target_job_id)
        dur = (time.time() - t0) * 1000
        self.tracer.emit("tool_call_completed", "OpsDeskAgent", f"查询日志 {target_job_id}", log_res, dur)

        log_failed = False
        if not log_res["ok"]:
            # 允许有限重试 1 次
            if log_res.get("retryable") and retry_counts.get("logs", 0) < 1:
                retry_counts["logs"] = retry_counts.get("logs", 0) + 1
                self.budget.record_tool_call()
                log_res = self.registry[TOOL_GET_JOB_LOGS](job_id=target_job_id)
            if not log_res["ok"]:
                log_failed = True
                collected_facts.append("日志查询超时未能取得内容")
        else:
            collected_evidence_ids.extend([e["id"] for e in log_res.get("evidence", [])])
            for log_item in log_res["data"].get("logs", []):
                if log_item["level"] == "ERROR":
                    collected_facts.append(f"日志记录: {log_item['message']}")

        # 第三步：若为日期/文件格式错误，调用 validate_csv
        csv_errors: list[dict[str, Any]] = []
        if file_id and error_code in ("INVALID_DATE", "MISSING_FIELD"):
            self.budget.record_step()
            self.budget.record_tool_call()
            t0 = time.time()
            csv_res = self.registry["validate_csv"](file_id=file_id)
            dur = (time.time() - t0) * 1000
            self.tracer.emit("tool_call_completed", "OpsDeskAgent", f"校验文件 {file_id}", csv_res, dur)
            if csv_res["ok"]:
                collected_evidence_ids.extend([e["id"] for e in csv_res.get("evidence", [])])
                csv_errors = csv_res["data"].get("errors", [])
                for err in csv_errors:
                    collected_facts.append(f"第 {err['row']} 行字段 {err['field']} 错误: {err['message']}")

        # 第四步：知识手册检索
        self.budget.record_step()
        self.budget.record_tool_call()
        t0 = time.time()
        kb_query = f"{error_code} 排查"
        kb_res = self.registry["search_runbooks"](query=kb_query, product_version=job_data.get("product_version", "v1"))
        dur = (time.time() - t0) * 1000
        self.tracer.emit("tool_call_completed", "OpsDeskAgent", f"检索规程: {kb_query}", kb_res, dur)
        if kb_res["ok"]:
            collected_evidence_ids.extend([e["id"] for e in kb_res.get("evidence", [])])

        # 3. 结果汇总与提案处理
        if log_failed:
            return {
                "run_id": self.ctx.run_id,
                "status": "needs_human_or_failed",
                "output": f"任务 {target_job_id} 错误码为 {error_code}，但日志未能取得（查询超时），未编造日志内容，建议人工介入核实底层状态。",
                "pending_question": None,
                "evidence_ids": list(set(collected_evidence_ids)),
                "facts": collected_facts,
                "action_proposal": None,
                "budget": self.budget.snapshot(),
            }

        # 判断是否需要生成工单提案（用户请求处理或发现不可自动修复数据）
        needs_ticket = "处理" in user_input or "工单" in user_input
        status = "succeeded"

        if needs_ticket:
            # 拟定提案（绝不直接调用 create_ticket）
            prop_id = f"PROP-{target_job_id.replace('IMP-', '')}-TICK"
            payload = {
                "title": f"支持工单: {target_job_id} 导入异常处理 ({error_code})",
                "priority": "P2",
                "description": f"经智能体排查发现：{'; '.join(collected_facts)}。需人工修正数据或后台修复。",
            }
            payload_str = json.dumps(payload, ensure_ascii=False, sort_keys=True)
            payload_hash = hashlib.sha256(payload_str.encode()).hexdigest()

            prop_repo = ActionProposalRepository(self.session)
            prop_obj = prop_repo.get_proposal(tenant_id=self.ctx.tenant_id, proposal_id=prop_id)
            if not prop_obj:
                prop_obj = prop_repo.create_proposal(
                    tenant_id=self.ctx.tenant_id,
                    proposal_id=prop_id,
                    action_type="create_ticket",
                    target_id=target_job_id,
                    target_version=job_data.get("version", 1),
                    payload_json=payload_str,
                    payload_hash=payload_hash,
                    created_by="OpsDeskAgent",
                )

            proposal_record = {
                "proposal_id": prop_obj.proposal_id,
                "action_type": prop_obj.action_type,
                "target_id": prop_obj.target_id,
                "status": prop_obj.status,
                "payload": payload,
                "payload_hash": payload_hash,
            }
            status = "waiting_approval"
            self.tracer.emit("proposal_created", "OpsDeskAgent", f"已生成工单提案草案 {prop_id}，等待人工审批", proposal_record)

        diagnosis_summary = f"任务 {target_job_id} 诊断完成：\n" + "\n".join(f"- {f}" for f in collected_facts)
        if status == "waiting_approval":
            diagnosis_summary += f"\n\n已为您拟定工单提案 [{proposal_record['proposal_id']}]，待审批人批准后即可创建。"

        self.tracer.emit("run_completed", "OpsDeskAgent", f"排查完成，状态: {status}")

        return {
            "run_id": self.ctx.run_id,
            "status": status,
            "output": diagnosis_summary,
            "pending_question": None,
            "evidence_ids": list(set(collected_evidence_ids)),
            "facts": collected_facts,
            "action_proposal": proposal_record,
            "budget": self.budget.snapshot(),
        }
