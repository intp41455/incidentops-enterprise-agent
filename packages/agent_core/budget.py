"""执行预算控制。限制模型轮次、工具调用数、费用、REWORK 次数，超限时转人工。"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any



class BudgetExceededException(Exception):
    pass


@dataclass
class ExecutionBudget:
    max_steps: int = 24
    max_tool_calls: int = 24
    max_rework_rounds: int = 2
    max_cost_cny: float = 2.0

    current_steps: int = 0
    current_tool_calls: int = 0
    current_rework_rounds: int = 0
    total_prompt_tokens: int = 0
    total_completion_tokens: int = 0
    total_cost_cny: float = 0.0

    def record_step(self) -> None:
        self.current_steps += 1
        if self.current_steps > self.max_steps:
            raise BudgetExceededException(f"已达到全局最大决策步数上限 ({self.max_steps})")

    def record_tool_call(self) -> None:
        self.current_tool_calls += 1
        if self.current_tool_calls > self.max_tool_calls:
            raise BudgetExceededException(f"已达到全局最大工具调用次数上限 ({self.max_tool_calls})")

    def record_rework_round(self) -> bool:
        """记录一次审查退回补查。若超过最大轮次，返回 False 触发转人工。"""
        self.current_rework_rounds += 1
        return self.current_rework_rounds <= self.max_rework_rounds

    def record_tokens(self, prompt_tokens: int, completion_tokens: int, cost_cny: float) -> None:
        self.total_prompt_tokens += prompt_tokens
        self.total_completion_tokens += completion_tokens
        self.total_cost_cny += cost_cny
        if self.total_cost_cny > self.max_cost_cny:
            raise BudgetExceededException(f"任务费用超过安全上限 ({self.total_cost_cny:.4f} > {self.max_cost_cny} 元)")

    def snapshot(self) -> dict[str, Any]:
        return {
            "steps": self.current_steps,
            "max_steps": self.max_steps,
            "tool_calls": self.current_tool_calls,
            "max_tool_calls": self.max_tool_calls,
            "rework_rounds": self.current_rework_rounds,
            "max_rework_rounds": self.max_rework_rounds,
            "prompt_tokens": self.total_prompt_tokens,
            "completion_tokens": self.total_completion_tokens,
            "cost_cny": round(self.total_cost_cny, 6),
        }
