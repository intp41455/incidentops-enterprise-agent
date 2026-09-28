"""手工冒烟：不接模型，直接调用两个工具并打印真实返回。

对应《每日开工与验收清单》第一天：
"不接模型，手工调用两个函数并核对返回值。"

用法（在 enterprise-agent-lab 下）：

    python scripts/smoke.py

会重建合成数据库（路径含 synthetic），然后依次调用两个工具。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "packages"))

from business_sim.db import (  # noqa: E402
    create_db_engine,
    create_session_factory,
    database_url,
)
from business_sim.identity import ToolContext, resolve_principal  # noqa: E402
from business_sim.seed import seed_synthetic_data  # noqa: E402
from business_sim.tools import build_tool_registry  # noqa: E402


def _dump(label: str, payload: dict) -> None:
    print(f"\n[smoke] {label}")
    print(json.dumps(payload, ensure_ascii=False, indent=2))


def main() -> int:
    url = database_url()
    print(f"[smoke] DATABASE_URL = {url}")

    engine = create_db_engine(url)
    counts = seed_synthetic_data(engine)
    print(f"[smoke] 合成数据已重置：{counts}")

    factory = create_session_factory(engine)
    with factory() as session:
        ctx_a = ToolContext(
            principal=resolve_principal(session, "u-a-viewer"),
            run_id="run-smoke-a",
        )
        tools_a = build_tool_registry(session, ctx_a)

        _dump(
            "get_import_job(job_id='IMP-104')  →  期望 error_code=INVALID_DATE",
            tools_a["get_import_job"](job_id="IMP-104"),
        )
        _dump(
            "get_job_logs(job_id='IMP-104')   →  期望第 3 行 start_date 报错，token 已脱敏",
            tools_a["get_job_logs"](job_id="IMP-104"),
        )
        _dump(
            "get_job_logs(job_id='IMP-107')   →  期望 TIMEOUT，且不返回任何日志内容",
            tools_a["get_job_logs"](job_id="IMP-107"),
        )
        _dump(
            "get_import_job(job_id='IMP-999') →  期望 NOT_FOUND_OR_FORBIDDEN",
            tools_a["get_import_job"](job_id="IMP-999"),
        )

        ctx_b = ToolContext(
            principal=resolve_principal(session, "u-b-viewer"),
            run_id="run-smoke-b",
        )
        tools_b = build_tool_registry(session, ctx_b)
        _dump(
            "跨租户：B 租户用户查 IMP-104  →  期望 NOT_FOUND_OR_FORBIDDEN 且不泄漏",
            tools_b["get_import_job"](job_id="IMP-104"),
        )

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
