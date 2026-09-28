"""D2 真实运行：由真实模型驱动单 Agent 完成一次导入失败排查，并留痕。

用法（在 enterprise-agent-lab 下）：

    python scripts/d2_real_run.py
    python scripts/d2_real_run.py "帮我看看 IMP-107 到底怎么了"

脚本会：
1. 读取 .env（不打印 Key）；
2. 重建合成数据库（路径含 synthetic，防呆见 D-003）；
3. 用真实模型跑一轮"模型决策 → 工具执行 → 观察回灌"；
4. 打印逐轮事件、真实 token 用量、费用状态；
5. 把 trace 写入 data/traces/。

**模型不可用时直接失败**，不产出任何替代结论（D-008）。
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "packages"))

DEFAULT_TASK = "导入任务 IMP-104 失败了，请查清楚失败原因，并说明依据。"


def load_env(path: Path) -> None:
    """极简 .env 解析：只处理 KEY=VALUE，已存在的环境变量优先。"""
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip())


def main() -> int:
    load_env(REPO_ROOT / ".env")
    # AGENTS.md 3.4：系统代理可能指向死端口，显式绕过本地
    os.environ.setdefault("NO_PROXY", "localhost,127.0.0.1,::1")

    from agent_core.budget import ExecutionBudget
    from agent_core.model_agent import ModelDrivenAgent
    from agent_core.model_client import ModelClient
    from agent_core.tracer import ExecutionTracer
    from business_sim.db import (
        create_db_engine,
        create_session_factory,
        database_url,
    )
    from business_sim.identity import ToolContext, resolve_principal
    from business_sim.seed import seed_synthetic_data

    task = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_TASK

    client = ModelClient()
    print(f"[d2] base_url = {client.base_url}")
    print(f"[d2] model    = {client.model_name}")
    print(f"[d2] 单价已核对 = {client.pricing_verified}")
    if not client.pricing_verified:
        print("[d2] 提示：未配置 PRICE_PER_1M_*_CNY，本次只记录真实 token，不折算金额。")

    url = database_url()
    print(f"[d2] DATABASE_URL = {url}")
    engine = create_db_engine(url)
    counts = seed_synthetic_data(engine)
    print(f"[d2] 合成数据已重置：{counts}")

    factory = create_session_factory(engine)
    with factory() as session:
        ctx = ToolContext(
            principal=resolve_principal(session, "u-a-operator"),
            run_id="run-d2-real",
        )
        tracer = ExecutionTracer(run_id=ctx.run_id)
        agent = ModelDrivenAgent(
            session=session,
            ctx=ctx,
            model_client=client,
            budget=ExecutionBudget(max_steps=8, max_tool_calls=10, max_cost_cny=2.0),
            tracer=tracer,
        )

        print(f"\n[d2] 任务：{task}\n")
        result = agent.run(task)

        print("[d2] —— 逐轮轨迹 ——")
        for event in tracer.events:
            print(f"  #{event.seq} [{event.event_type}] {event.summary}")
            if event.event_type == "tool_call_completed":
                payload = event.details.get("result", {})
                print(f"      工具 {event.details.get('tool_call_id')} ok={payload.get('ok')}")
            if event.event_type == "model_decision":
                print(
                    f"      tokens: prompt={event.details.get('prompt_tokens')} "
                    f"completion={event.details.get('completion_tokens')} "
                    f"source={event.details.get('usage_source')}"
                )

        print("\n[d2] —— 最终结果 ——")
        print(f"  状态：{result['status']}")
        print(f"  模型轮次：{result['rounds']}，工具调用：{result['tool_calls_count']} {result['tool_calls']}")
        print(f"  证据：{result['evidence_ids']}")
        print(f"  usage_source：{result['usage_source']}，单价已核对：{result['pricing_verified']}")
        print(f"  真实 token：prompt={result['prompt_tokens']} completion={result['completion_tokens']}")
        if result["pricing_verified"]:
            print(f"  本次费用：{result['cost_cny']} 元")
        else:
            print("  本次费用：未核对单价，未折算金额（不计入预算消耗）")
        print("\n  答复：")
        print("  " + (result["output"] or "").replace("\n", "\n  "))

        trace_path = tracer.save_trace()
        print(f"\n[d2] trace 已写入：{trace_path}")

        if result["status"] != "answered":
            print("\n[d2] 未取得模型答复，按规则不得声称已完成。")
            return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())