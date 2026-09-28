"""FixedPipeline 固定流水线（Baseline B）。

用于同题 A/B/C 对照实验：
- 严格按照固定顺序执行：查任务 -> 查日志 -> 查文件 -> 检索规程；
- 无 ReviewAgent 独立复核；
- 无退回补查循环（REWORK）；
- 无前置依赖动态验证与冲突阻断。
"""

from __future__ import annotations

import re
import time
from typing import Any, Optional

from sqlalchemy.orm import Session

from agent_core.budget import ExecutionBudget
from agent_core.tracer import ExecutionTracer
from business_sim.identity import ToolContext
from business_sim.tools import build_tool_registry


class FixedPipeline:
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

    def run(self, user_input: str) -> dict[str, Any]:
        self.tracer.emit("agent_started", "FixedPipeline", f"固定流水线启动: {user_input}")

        job_ids = re.findall(r"IMP-[A-Za-z0-9]+", user_input)
        if not job_ids:
            return {
                "run_id": self.ctx.run_id,
                "status": "waiting_user",
                "output": "请提供具体的导入任务 ID",
                "pending_question": "请提供具体的导入任务 ID",
                "evidence_ids": [],
                "facts": [],
                "action_proposal": None,
                "budget": self.budget.snapshot(),
            }

        facts: list[str] = []
        evidence_ids: list[str] = []

        # 固定顺序流转
        for jid in job_ids:
            self.budget.record_step()
            self.budget.record_tool_call()
            jres = self.tools["get_import_job"](job_id=jid)
            if not jres["ok"]:
                return {
                    "run_id": self.ctx.run_id,
                    "status": "needs_human_or_denied",
                    "output": jres["error"]["message"],
                    "pending_question": None,
                    "evidence_ids": [],
                    "facts": [],
                    "action_proposal": None,
                    "budget": self.budget.snapshot(),
                }
            evidence_ids.extend([e["id"] for e in jres.get("evidence", [])])
            jdata = jres["data"]
            err_code = jdata.get("error_code")
            facts.append(f"任务 {jid} 错误码: {err_code}")

            # 日志
            self.budget.record_step()
            self.budget.record_tool_call()
            lres = self.tools["get_job_logs"](job_id=jid)
            if lres["ok"]:
                evidence_ids.extend([e["id"] for e in lres.get("evidence", [])])
            else:
                facts.append(f"任务 {jid} 日志超时")

            # 盲目推断：如果用户输入带'重试'，流水线直接建议重试，不做前置幂等检查！
            if "重试" in user_input and err_code == "UPSTREAM_TIMEOUT":
                facts.append(f"流水线直接建议对任务 {jid} 进行重试（未核对前置幂等状态）")

        self.tracer.emit("run_completed", "FixedPipeline", "固定流水线执行完毕")

        return {
            "run_id": self.ctx.run_id,
            "status": "succeeded",
            "output": "流水线排查完成：\n" + "\n".join(f"- {f}" for f in facts),
            "pending_question": None,
            "evidence_ids": list(set(evidence_ids)),
            "facts": facts,
            "action_proposal": None,
            "budget": self.budget.snapshot(),
        }
