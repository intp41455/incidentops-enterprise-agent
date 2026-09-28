"""可观察性与审计追踪器（Tracer）。

严格对应开工手册 §6.5、§7.4 与决策记录：
1. 记录每一次智能体决策、工具调用、输入输出参数摘要、耗时与证据 ID；
2. 记录 Token 与费用明细；
3. 支持流式 SSE 订阅（前端实时展示工作台执行拓扑）；
4. 支持持久化导出为 trace.jsonl。
"""

from __future__ import annotations

import json
import os
import time
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime
from typing import Any, Callable, Optional


@dataclass
class TraceEvent:
    event_id: str
    run_id: str
    seq: int
    ts: str
    event_type: str
    agent_role: str
    summary: str
    details: dict[str, Any] = field(default_factory=dict)
    duration_ms: float = 0.0


class ExecutionTracer:
    def __init__(self, run_id: str, trace_dir: Optional[str] = None) -> None:
        self.run_id = run_id
        self.trace_dir = trace_dir or "data/traces"
        self.events: list[TraceEvent] = []
        self._seq = 0
        self._subscribers: list[Callable[[dict[str, Any]], None]] = []

    def add_subscriber(self, callback: Callable[[dict[str, Any]], None]) -> None:
        self._subscribers.append(callback)

    def emit(
        self,
        event_type: str,
        agent_role: str,
        summary: str,
        details: Optional[dict[str, Any]] = None,
        duration_ms: float = 0.0,
    ) -> TraceEvent:
        self._seq += 1
        event = TraceEvent(
            event_id=f"evt-{uuid.uuid4().hex[:8]}",
            run_id=self.run_id,
            seq=self._seq,
            ts=datetime.utcnow().isoformat(),
            event_type=event_type,
            agent_role=agent_role,
            summary=summary,
            details=details or {},
            duration_ms=round(duration_ms, 2),
        )
        self.events.append(event)

        # 通知实时订阅者（如 SSE 流）
        event_dict = asdict(event)
        for sub in self._subscribers:
            try:
                sub(event_dict)
            except Exception:
                pass

        return event

    def save_trace(self) -> str:
        os.makedirs(self.trace_dir, exist_ok=True)
        file_path = os.path.join(self.trace_dir, f"{self.run_id}_trace.jsonl")
        with open(file_path, "w", encoding="utf-8") as f:
            for event in self.events:
                f.write(json.dumps(asdict(event), ensure_ascii=False) + "\n")
        return file_path

    def get_summary(self) -> dict[str, Any]:
        tool_calls = [e for e in self.events if e.event_type == "tool_call_completed"]
        rework_events = [e for e in self.events if e.event_type == "rework_requested"]
        return {
            "run_id": self.run_id,
            "total_events": len(self.events),
            "tool_calls_count": len(tool_calls),
            "rework_count": len(rework_events),
            "events": [asdict(e) for e in self.events],
        }
