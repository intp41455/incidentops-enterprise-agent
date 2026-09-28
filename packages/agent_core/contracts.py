"""智能体间交接协议与数据契约（contracts）。

严格对应开工手册 §7.4 与 FindYourself 规格 §3：
- TaskEnvelope: 子任务派发包（限定上下文与工具边界）
- FactClaim: 技术事实（必须挂载真实 evidence ID）
- Hypothesis: 未证假设
- AgentResult: 专家智能体结构化输出
- ActionProposalDraft: 待审批动作提案草案
- ReviewVerdict: 审查智能体裁决（通过 / 退回补查 / 转人工）
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Optional


@dataclass
class TaskEnvelope:
    task_id: str
    goal: str
    from_agent: str
    to_agent: str
    scope: dict[str, Any]  # 如 {"job_ids": ["IMP-104"]}，服务端强制租户过滤
    allowed_tools: list[str]
    context_refs: list[str] = field(default_factory=list)
    remaining_budget_steps: int = 4
    created_at: str = field(default_factory=lambda: datetime.utcnow().isoformat())


@dataclass
class FactClaim:
    claim: str
    evidence_ids: list[str] = field(default_factory=list)
    source_tool: str = ""


@dataclass
class Hypothesis:
    claim: str
    status: str = "unconfirmed"  # unconfirmed | confirmed | rejected
    counter_evidence: list[str] = field(default_factory=list)


@dataclass
class ActionProposalDraft:
    action_type: str  # create_ticket | retry_import_job
    target_id: str
    target_version: int
    payload: dict[str, Any]
    required_prerequisites: list[str] = field(default_factory=list)
    risk_level: str = "low"  # low | medium | high


@dataclass
class AgentResult:
    task_id: str
    agent_role: str
    status: str  # completed | needs_more_evidence | error | refused
    summary: str
    facts: list[FactClaim] = field(default_factory=list)
    hypotheses: list[Hypothesis] = field(default_factory=list)
    missing_information: list[str] = field(default_factory=list)
    recommended_next_tool: Optional[str] = None
    action_proposal: Optional[ActionProposalDraft] = None
    evidence_ids: list[str] = field(default_factory=list)
    prompt_tokens: int = 0
    completion_tokens: int = 0
    cost_cny: float = 0.0


@dataclass
class ReviewVerdict:
    # PASS: 证据充分且合规；REWORK: 证据缺失或冲突退回补查；ESCALATE: 无法消解矛盾转人工
    decision: str  # PASS | REWORK | ESCALATE
    reason: str
    rework_target_agent: Optional[str] = None  # diagnosis | remediation
    missing_evidence: list[str] = field(default_factory=list)
    contradictions: list[str] = field(default_factory=list)
    approved_proposal: Optional[ActionProposalDraft] = None
