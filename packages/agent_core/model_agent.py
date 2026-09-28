"""真实 LLM 驱动的单 Agent 工具循环。

和 `single_agent/agent.py` 的区别：那个是确定性脚本（用来跑测试/回归），
这个走真实模型 tool_calls。"""

from __future__ import annotations

import json
from typing import Any, Optional

from sqlalchemy.orm import Session

from .budget import BudgetExceededException, ExecutionBudget
from .model_client import ModelClient, ModelUnavailableError
from .tracer import ExecutionTracer

from business_sim.identity import ToolContext
from business_sim.tools import TOOL_SCHEMAS, build_tool_registry

SYSTEM_PROMPT = """你是 IncidentOps 平台的运维支持智能体 OpsDesk，负责定位企业 CSV 导入失败的原因。

工作要求：
1. 只依据工具返回的真实数据作答，不得凭常识猜测字段、行号或错误码。
2. 需要数据时必须调用工具，不要向用户索要你可以自己查到的信息。
3. 工具报错（如 TIMEOUT）时如实说明"未能取得数据"，不得编造日志内容。
4. 回答用中文，结构为：结论 → 依据（引用 evidence id）→ 建议下一步。
5. 你没有写权限：涉及创建工单等写操作时只能建议，不能宣称已执行。
"""


class ModelDrivenAgent:
    """单 Agent：模型决策 + 工具执行 + 观察回灌，直到模型给出最终答复。"""

    def __init__(
        self,
        session: Session,
        ctx: ToolContext,
        model_client: Optional[ModelClient] = None,
        budget: Optional[ExecutionBudget] = None,
        tracer: Optional[ExecutionTracer] = None,
        max_rounds: int = 8,
        tool_names: Optional[list[str]] = None,
    ) -> None:
        self.session = session
        self.ctx = ctx
        self.client = model_client or ModelClient()
        self.budget = budget or ExecutionBudget()
        self.tracer = tracer or ExecutionTracer(run_id=ctx.run_id)
        self.max_rounds = max_rounds

        registry = build_tool_registry(session, ctx)
        selected = tool_names or list(TOOL_SCHEMAS.keys())
        self.tool_specs = [TOOL_SCHEMAS[name] for name in selected if name in TOOL_SCHEMAS]
        self.registry = {name: registry[name] for name in selected if name in registry}

    def run(self, user_input: str) -> dict[str, Any]:
        messages: list[dict[str, Any]] = [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": user_input},
        ]
        evidence_ids: list[str] = []
        executed_tools: list[str] = []
        usage_sources: set[str] = set()
        pricing_verified = True

        outcome = "max_rounds_reached"
        final_text = ""

        try:
            for round_index in range(1, self.max_rounds + 1):
                self.budget.record_step()
                t0 = _now_ms()
                resp = self.client.chat_completion(
                    messages=messages, tools=self.tool_specs
                )
                self.budget.record_tokens(
                    resp["prompt_tokens"], resp["completion_tokens"], resp["cost_cny"]
                )
                usage_sources.add(resp.get("usage_source", "unknown"))
                pricing_verified = pricing_verified and bool(
                    resp.get("pricing_verified", False)
                )
                self.tracer.emit(
                    "model_decision",
                    "OpsDeskAgent",
                    f"第 {round_index} 轮模型决策："
                    + (f"请求调用 {len(resp['tool_calls'])} 个工具" if resp["tool_calls"] else "给出最终答复"),
                    {
                        "round": round_index,
                        "tool_calls": resp["tool_calls"],
                        "prompt_tokens": resp["prompt_tokens"],
                        "completion_tokens": resp["completion_tokens"],
                        "usage_source": resp.get("usage_source"),
                    },
                    _now_ms() - t0,
                )

                if not resp["tool_calls"]:
                    final_text = resp["content"]
                    outcome = "answered"
                    break

                messages.append(_assistant_tool_call_message(resp))
                for call in resp["tool_calls"]:
                    self.budget.record_tool_call()
                    result, duration = self._execute(call)
                    executed_tools.append(call["name"])
                    evidence_ids.extend(
                        e["id"] for e in (result.get("evidence") or [])
                    )
                    self.tracer.emit(
                        "tool_call_completed",
                        "OpsDeskAgent",
                        f"执行工具 {call['name']}",
                        {"tool_call_id": call["id"], "arguments": call["arguments"], "result": result},
                        duration,
                    )
                    messages.append(
                        {
                            "role": "tool",
                            "tool_call_id": call["id"],
                            "content": json.dumps(result, ensure_ascii=False),
                        }
                    )
        except ModelUnavailableError as exc:
            outcome = "model_unavailable"
            final_text = f"模型不可用，未产生任何结论：{exc}"
            self.tracer.emit("run_failed", "OpsDeskAgent", final_text)
        except BudgetExceededException as exc:
            outcome = "budget_exceeded"
            final_text = f"安全预算触发熔断，已停止执行：{exc}"
            self.tracer.emit("run_failed", "OpsDeskAgent", final_text)

        if outcome == "max_rounds_reached":
            final_text = f"达到最大模型轮次上限（{self.max_rounds}），未收敛。"

        snapshot = self.budget.snapshot()
        self.tracer.emit("run_completed", "OpsDeskAgent", f"结束状态：{outcome}", snapshot)

        return {
            "run_id": self.ctx.run_id,
            "status": outcome,
            "output": final_text,
            "rounds": snapshot["steps"],
            "tool_calls": executed_tools,
            "tool_calls_count": len(executed_tools),
            "evidence_ids": sorted(set(evidence_ids)),
            "usage_source": sorted(usage_sources),
            "pricing_verified": pricing_verified,
            "prompt_tokens": snapshot["prompt_tokens"],
            "completion_tokens": snapshot["completion_tokens"],
            "cost_cny": snapshot["cost_cny"],
            "budget": snapshot,
        }

    # --- 工具执行 -----------------------------------------------------------

    def _execute(self, call: dict[str, Any]) -> tuple[dict[str, Any], float]:
        name = call["name"]
        args = call.get("arguments") or {}
        t0 = _now_ms()

        handler = self.registry.get(name)
        if handler is None:
            return (
                {
                    "ok": False,
                    "data": None,
                    "evidence": [],
                    "error": {"code": "UNKNOWN_TOOL", "message": f"未开放的工具：{name}"},
                    "retryable": False,
                },
                _now_ms() - t0,
            )
        if not isinstance(args, dict):
            return (
                {
                    "ok": False,
                    "data": None,
                    "evidence": [],
                    "error": {"code": "INVALID_ARGUMENT", "message": "工具参数不是对象"},
                    "retryable": False,
                },
                _now_ms() - t0,
            )
        try:
            result = handler(**args)
        except TypeError as exc:
            result = {
                "ok": False,
                "data": None,
                "evidence": [],
                "error": {"code": "INVALID_ARGUMENT", "message": f"工具参数不匹配：{exc}"},
                "retryable": False,
            }
        except Exception as exc:  # 工具内部异常不得让整轮崩溃
            result = {
                "ok": False,
                "data": None,
                "evidence": [],
                "error": {"code": "TOOL_EXECUTION_ERROR", "message": type(exc).__name__},
                "retryable": False,
            }
        return result, _now_ms() - t0


def _assistant_tool_call_message(resp: dict[str, Any]) -> dict[str, Any]:
    return {
        "role": "assistant",
        "content": resp.get("content") or None,
        "tool_calls": [
            {
                "id": c["id"],
                "type": "function",
                "function": {
                    "name": c["name"],
                    "arguments": json.dumps(c.get("arguments") or {}, ensure_ascii=False),
                },
            }
            for c in resp["tool_calls"]
        ],
    }


def _now_ms() -> float:
    import time

    return time.perf_counter() * 1000.0