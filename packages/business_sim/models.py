"""业务表 ORM 模型。所有业务表都带 tenant_id。"""

from __future__ import annotations

from datetime import datetime
from typing import Optional

from sqlalchemy import DateTime, ForeignKey, Integer, String, Text
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


class Tenant(Base):
    __tablename__ = "tenants"

    tenant_id: Mapped[str] = mapped_column(String(32), primary_key=True)
    name: Mapped[str] = mapped_column(String(128))
    created_at: Mapped[datetime] = mapped_column(DateTime)


class User(Base):
    __tablename__ = "users"

    user_id: Mapped[str] = mapped_column(String(32), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(
        ForeignKey("tenants.tenant_id"), index=True, nullable=False
    )
    display_name: Mapped[str] = mapped_column(String(64))
    # viewer | operator | approver
    role: Mapped[str] = mapped_column(String(16), nullable=False)


class FileRecord(Base):
    __tablename__ = "files"

    file_id: Mapped[str] = mapped_column(String(32), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(
        ForeignKey("tenants.tenant_id"), index=True, nullable=False
    )
    filename: Mapped[str] = mapped_column(String(256))
    row_count: Mapped[int] = mapped_column(Integer, default=0)
    size_bytes: Mapped[int] = mapped_column(Integer, default=0)
    # 合成 CSV 的样例行（JSON 文本）。用于后续 validate_csv 与提示注入测试。
    sample_rows_json: Mapped[Optional[str]] = mapped_column(Text, nullable=True)


class ImportJob(Base):
    __tablename__ = "import_jobs"

    job_id: Mapped[str] = mapped_column(String(32), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(
        ForeignKey("tenants.tenant_id"), index=True, nullable=False
    )
    file_id: Mapped[str] = mapped_column(String(32))
    # succeeded | failed | running
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    error_code: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)
    product_version: Mapped[str] = mapped_column(String(16), default="v1")
    manual_version: Mapped[str] = mapped_column(String(16), default="v1")
    # 业务对象版本：供后续审批绑定（参数或状态变化后旧批准失效）
    version: Mapped[int] = mapped_column(Integer, default=1)
    created_at: Mapped[datetime] = mapped_column(DateTime)


class JobLog(Base):
    __tablename__ = "job_logs"

    log_id: Mapped[str] = mapped_column(String(48), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(
        ForeignKey("tenants.tenant_id"), index=True, nullable=False
    )
    job_id: Mapped[str] = mapped_column(String(32), index=True, nullable=False)
    ts: Mapped[datetime] = mapped_column(DateTime)
    level: Mapped[str] = mapped_column(String(8))
    message: Mapped[str] = mapped_column(Text)
    record_id: Mapped[str] = mapped_column(String(32))


class SyntheticFault(Base):
    """合成故障开关（**测试夹具，不是业务表**）。

    按 (tool_name, target_id) 注入 TIMEOUT 等故障，让失败路径由数据触发，
    而不是写死在代码里。只存在于合成环境，接真实系统前必须整表移除。
    （合成测试数据，接真实系统前移除。）
    """

    __tablename__ = "synthetic_faults"

    fault_id: Mapped[str] = mapped_column(String(48), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(
        ForeignKey("tenants.tenant_id"), index=True, nullable=False
    )
    tool_name: Mapped[str] = mapped_column(String(32), nullable=False)
    target_id: Mapped[str] = mapped_column(String(32), index=True, nullable=False)
    # TIMEOUT | ...
    failure: Mapped[str] = mapped_column(String(16), nullable=False)


class ActionProposal(Base):
    """待审批的操作提案（不可变）。

    写动作（创建工单、重试导入）必须先形成提案，展示具体参数与变更；
    审批通过后，执行器校验 payload_hash 与业务对象版本，防止参数篡改或状态过期。
    """

    __tablename__ = "action_proposals"

    proposal_id: Mapped[str] = mapped_column(String(48), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(
        ForeignKey("tenants.tenant_id"), index=True, nullable=False
    )
    action_type: Mapped[str] = mapped_column(String(32), nullable=False)
    target_id: Mapped[str] = mapped_column(String(32), index=True, nullable=False)
    target_version: Mapped[int] = mapped_column(Integer, default=1)
    payload_json: Mapped[str] = mapped_column(Text, nullable=False)
    payload_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    # pending | approved | rejected | executed
    status: Mapped[str] = mapped_column(String(16), default="pending")
    created_by: Mapped[str] = mapped_column(String(32), default="system")
    created_at: Mapped[datetime] = mapped_column(DateTime)


class ApprovalRecord(Base):
    """操作审批审计记录。"""

    __tablename__ = "approvals"

    approval_id: Mapped[str] = mapped_column(String(48), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(
        ForeignKey("tenants.tenant_id"), index=True, nullable=False
    )
    proposal_id: Mapped[str] = mapped_column(
        ForeignKey("action_proposals.proposal_id"), index=True, nullable=False
    )
    approver_id: Mapped[str] = mapped_column(
        ForeignKey("users.user_id"), nullable=False
    )
    decision: Mapped[str] = mapped_column(String(16), nullable=False)  # approved | rejected
    payload_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime)


class TicketRecord(Base):
    """支持工单记录，具备全局唯一的 idempotency_key 保证幂等写入。"""

    __tablename__ = "tickets"

    ticket_id: Mapped[str] = mapped_column(String(32), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(
        ForeignKey("tenants.tenant_id"), index=True, nullable=False
    )
    proposal_id: Mapped[Optional[str]] = mapped_column(String(48), nullable=True)
    title: Mapped[str] = mapped_column(String(256), nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False)
    priority: Mapped[str] = mapped_column(String(16), default="P2")
    status: Mapped[str] = mapped_column(String(16), default="open")  # open | in_progress | resolved
    idempotency_key: Mapped[str] = mapped_column(String(64), unique=True, index=True, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime)


class PrerequisiteRecord(Base):
    """模拟业务前置条件状态（如：上游服务是否恢复、是否已存在重复写入等）。"""

    __tablename__ = "prerequisites"

    prereq_id: Mapped[str] = mapped_column(String(48), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(
        ForeignKey("tenants.tenant_id"), index=True, nullable=False
    )
    target_id: Mapped[str] = mapped_column(String(32), index=True, nullable=False)
    check_type: Mapped[str] = mapped_column(String(32), nullable=False)
    # passed | failed | unknown
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    details: Mapped[str] = mapped_column(Text, default="")

