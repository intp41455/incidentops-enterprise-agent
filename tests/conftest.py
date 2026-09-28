"""测试夹具：内存 SQLite + 合成数据。

不依赖模型、不联网、不写文件。身份通过 `resolve_principal` 的占位实现构造
（真实认证由 D2 提供）。
"""

from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
PACKAGES_DIR = REPO_ROOT / "packages"
if str(PACKAGES_DIR) not in sys.path:
    sys.path.insert(0, str(PACKAGES_DIR))

import pytest  # noqa: E402
from sqlalchemy.pool import StaticPool  # noqa: E402

from business_sim.db import (  # noqa: E402
    create_db_engine,
    create_session_factory,
    init_db,
)
from business_sim.identity import ToolContext, resolve_principal  # noqa: E402
from business_sim.seed import seed_synthetic_data  # noqa: E402


@pytest.fixture()
def engine():
    # StaticPool 让内存库在多个 Session 之间共享同一条连接
    eng = create_db_engine("sqlite://", poolclass=StaticPool)
    init_db(eng)
    seed_synthetic_data(eng)
    return eng


@pytest.fixture()
def session(engine):
    factory = create_session_factory(engine)
    with factory() as s:
        yield s


def _ctx(session, user_id: str, run_id: str) -> ToolContext:
    return ToolContext(
        principal=resolve_principal(session, user_id),
        run_id=run_id,
    )


@pytest.fixture()
def ctx_a_viewer(session):
    return _ctx(session, "u-a-viewer", "run-test-a-viewer")


@pytest.fixture()
def ctx_a_operator(session):
    return _ctx(session, "u-a-operator", "run-test-a-operator")


@pytest.fixture()
def ctx_a_approver(session):
    return _ctx(session, "u-a-approver", "run-test-a-approver")


@pytest.fixture()
def ctx_b_viewer(session):
    return _ctx(session, "u-b-viewer", "run-test-b-viewer")
