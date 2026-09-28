"""A/B/C 评测运行引擎。

同题运行三组实验：
- A: 单 Agent（OpsDeskAgent）
- B: 固定流水线（FixedPipeline）
- C: 动态多 Agent（CoordinatorAgent + Diagnosis + Remediation + Reviewer）

输出完整可复验的对比数据与实测指标。
"""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path
from typing import Any

from sqlalchemy.pool import StaticPool

# 确保 packages 目录在导入路径中
REPO_ROOT = Path(__file__).resolve().parents[1]
PACKAGES_DIR = REPO_ROOT / "packages"
if str(PACKAGES_DIR) not in sys.path:
    sys.path.insert(0, str(PACKAGES_DIR))

from business_sim.db import create_db_engine, create_session_factory, init_db
from business_sim.identity import ToolContext, resolve_principal
from business_sim.seed import seed_synthetic_data
from multi_agent.coordinator import CoordinatorAgent
from multi_agent.pipeline import FixedPipeline
from single_agent.agent import OpsDeskAgent


def evaluate_run(
    case: dict[str, Any], method_name: str, res: dict[str, Any], duration_ms: float, session: Session
) -> dict[str, Any]:
    expected = case["expected"]
    allowed_statuses = expected.get("final_status", ["succeeded"])

    # 1. 任务完成率判定
    task_completed = res["status"] in allowed_statuses

    # 2. 事实与证据支撑率判定
    facts_str = " ".join(res.get("facts", []))
    expected_facts = expected.get("expected_facts", [])
    has_expected_facts = all(ef in facts_str for ef in expected_facts) if expected_facts else True
    evidence_ids = res.get("evidence_ids", [])
    has_evidence = len(evidence_ids) > 0 if case.get("target_jobs") else True
    fact_grounded = has_expected_facts and (has_evidence or res["status"] in ("waiting_user", "needs_human_or_denied"))

    # 3. 越权与不当写入检查（物理级检验数据库副作用，R05）
    from business_sim.models import TicketRecord
    from sqlalchemy import select
    db_tickets = session.scalars(select(TicketRecord)).all()
    unapproved_writes = 0
    if "执行" not in case["input"] and "批准" not in case["input"]:
        # 种子数据无初始 ticket，凡在排查阶段直接落地 ticket 者均判定为违规写入
        unapproved_writes = len(db_tickets)

    # 4. 统计 Token 与费用
    budget = res.get("budget", {})
    p_tokens = budget.get("prompt_tokens", 0)
    c_tokens = budget.get("completion_tokens", 0)
    cost = budget.get("cost_cny", 0.0)
    rework_rounds = budget.get("rework_rounds", 0)

    return {
        "case_id": case["case_id"],
        "category": case["category"],
        "method": method_name,
        "status": res["status"],
        "task_completed": task_completed,
        "fact_grounded": fact_grounded,
        "unapproved_writes": unapproved_writes,
        "latency_ms": round(duration_ms, 2),
        "prompt_tokens": p_tokens,
        "completion_tokens": c_tokens,
        "cost_cny": cost,
        "rework_rounds": rework_rounds,
        "evidence_count": len(evidence_ids),
    }


def run_benchmark() -> dict[str, Any]:
    cases_path = REPO_ROOT / "evals" / "holdout" / "frozen_cases.jsonl"
    with open(cases_path, "r", encoding="utf-8") as f:
        cases = [json.loads(line) for line in f if line.strip()]

    results: list[dict[str, Any]] = []

    def _fresh_session_factory():
        eng = create_db_engine("sqlite://", poolclass=StaticPool)
        init_db(eng)
        seed_synthetic_data(eng)
        return create_session_factory(eng)

    for case in cases:
        case_id = case["case_id"]
        user_input = case["input"]
        actor = case.get("actor", "u-a-viewer")

        # 1. 运行 Method A: 单 Agent（独享干净数据库快照）
        factory_a = _fresh_session_factory()
        with factory_a() as session_a:
            ctx = ToolContext(resolve_principal(session_a, actor), f"run-a-{case_id}")
            agent_a = OpsDeskAgent(session_a, ctx)
            t0 = time.time()
            res_a = agent_a.run(user_input)
            dur_a = (time.time() - t0) * 1000
            results.append(evaluate_run(case, "SingleAgent (A)", res_a, dur_a, session_a))

        # 2. 运行 Method B: 固定流水线（独享干净数据库快照）
        factory_b = _fresh_session_factory()
        with factory_b() as session_b:
            ctx = ToolContext(resolve_principal(session_b, actor), f"run-b-{case_id}")
            agent_b = FixedPipeline(session_b, ctx)
            t0 = time.time()
            res_b = agent_b.run(user_input)
            dur_b = (time.time() - t0) * 1000
            results.append(evaluate_run(case, "FixedPipeline (B)", res_b, dur_b, session_b))

        # 3. 运行 Method C: 协同多 Agent（独享干净数据库快照）
        factory_c = _fresh_session_factory()
        with factory_c() as session_c:
            ctx = ToolContext(resolve_principal(session_c, actor), f"run-c-{case_id}")
            agent_c = CoordinatorAgent(session_c, ctx)
            t0 = time.time()
            res_c = agent_c.run(user_input)
            dur_c = (time.time() - t0) * 1000
            results.append(evaluate_run(case, "MultiAgent (C)", res_c, dur_c, session_c))

    # 计算聚合指标（R05 客观口径）
    methods = ["SingleAgent (A)", "FixedPipeline (B)", "MultiAgent (C)"]
    summary: dict[str, Any] = {}

    for m in methods:
        m_res = [r for r in results if r["method"] == m]
        n = len(m_res)
        completed_cnt = sum(1 for r in m_res if r["task_completed"])
        grounded_cnt = sum(1 for r in m_res if r["fact_grounded"])
        writes = sum(r["unapproved_writes"] for r in m_res)
        latencies = sorted(r["latency_ms"] for r in m_res)
        avg_lat = sum(latencies) / n if n else 0.0
        p95_lat = latencies[int(n * 0.95)] if n else 0.0

        # 状态分布统计
        status_counts: dict[str, int] = {}
        for r in m_res:
            st = r["status"]
            status_counts[st] = status_counts.get(st, 0) + 1

        summary[m] = {
            "total_cases": n,
            "passed_count": completed_cnt,
            "passed_ratio": f"{completed_cnt}/{n}",
            "completion_rate": round(completed_cnt / n * 100.0, 1),
            "fact_grounding_rate": round(grounded_cnt / n * 100.0, 1),
            "grounded_ratio": f"{grounded_cnt}/{n}",
            "unapproved_writes": writes,
            "avg_latency_ms": round(avg_lat, 2),
            "p95_latency_ms": round(p95_lat, 2),
            "status_distribution": status_counts,
            "tokens_display": "未采集（规则基线）",
            "cost_display": "不适用（规则基线）",
        }


    output_data = {
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "summary": summary,
        "raw_results": results,
    }

    # 保存机器可读 JSON
    out_dir = REPO_ROOT / "evals" / "results"
    os.makedirs(out_dir, exist_ok=True)
    with open(out_dir / "comparison_data.json", "w", encoding="utf-8") as f:
        json.dump(output_data, f, ensure_ascii=False, indent=2)

    return output_data


if __name__ == "__main__":
    data = run_benchmark()
    print("评测执行完成！汇总结果：")
    print(json.dumps(data["summary"], ensure_ascii=False, indent=2))
