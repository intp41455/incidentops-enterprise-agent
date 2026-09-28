"""仓储层。所有业务查询强制 keyword-only 传入 tenant_id，
跨租户统一返回 None，避免泄漏目标是否存在。"""

from __future__ import annotations

from datetime import datetime
from typing import Optional, Sequence

from sqlalchemy import select
from sqlalchemy.orm import Session

from .models import (
    ActionProposal,
    ApprovalRecord,
    FileRecord,
    ImportJob,
    JobLog,
    PrerequisiteRecord,
    SyntheticFault,
    Tenant,
    TicketRecord,
    User,
)


class TenantRepository:
    def __init__(self, session: Session) -> None:
        self._session = session

    def exists(self, *, tenant_id: str) -> bool:
        stmt = select(Tenant.tenant_id).where(Tenant.tenant_id == tenant_id)
        return self._session.scalars(stmt).one_or_none() is not None


class UserRepository:
    def __init__(self, session: Session) -> None:
        self._session = session

    def get_user(self, *, tenant_id: str, user_id: str) -> Optional[User]:
        stmt = select(User).where(User.tenant_id == tenant_id, User.user_id == user_id)
        return self._session.scalars(stmt).one_or_none()

    def get_user_by_id(self, user_id: str) -> Optional[User]:
        """**仅限服务端身份解析使用**，不带租户过滤。

        这是本仓库唯一一个不带 tenant_id 的查询：身份解析发生在租户确定**之前**。
        因此它不得暴露给工具层或模型。
        """
        stmt = select(User).where(User.user_id == user_id)
        return self._session.scalars(stmt).one_or_none()


class ImportJobRepository:
    def __init__(self, session: Session) -> None:
        self._session = session

    def get_job(self, *, tenant_id: str, job_id: str) -> Optional[ImportJob]:
        stmt = select(ImportJob).where(
            ImportJob.tenant_id == tenant_id,
            ImportJob.job_id == job_id,
        )
        return self._session.scalars(stmt).one_or_none()

    def list_jobs(self, *, tenant_id: str) -> Sequence[ImportJob]:
        """列出本租户的失败任务。用于"缺信息追问"时给出候选（不含其他租户）。"""
        stmt = (
            select(ImportJob)
            .where(ImportJob.tenant_id == tenant_id, ImportJob.status == "failed")
            .order_by(ImportJob.job_id)
        )
        return self._session.scalars(stmt).all()


class JobLogRepository:
    def __init__(self, session: Session) -> None:
        self._session = session

    def list_logs(
        self, *, tenant_id: str, job_id: str, limit: int
    ) -> Sequence[JobLog]:
        stmt = (
            select(JobLog)
            .where(JobLog.tenant_id == tenant_id, JobLog.job_id == job_id)
            .order_by(JobLog.ts, JobLog.log_id)
            .limit(limit)
        )
        return self._session.scalars(stmt).all()

    def count_logs(self, *, tenant_id: str, job_id: str) -> int:
        stmt = select(JobLog).where(
            JobLog.tenant_id == tenant_id, JobLog.job_id == job_id
        )
        return len(self._session.scalars(stmt).all())

    def get_log(self, *, tenant_id: str, log_id: str) -> Optional[JobLog]:
        stmt = select(JobLog).where(
            JobLog.tenant_id == tenant_id, JobLog.log_id == log_id
        )
        return self._session.scalars(stmt).one_or_none()


class FileRepository:
    def __init__(self, session: Session) -> None:
        self._session = session

    def get_file(self, *, tenant_id: str, file_id: str) -> Optional[FileRecord]:
        stmt = select(FileRecord).where(
            FileRecord.tenant_id == tenant_id,
            FileRecord.file_id == file_id,
        )
        return self._session.scalars(stmt).one_or_none()


class SyntheticFaultRepository:
    def __init__(self, session: Session) -> None:
        self._session = session

    def find(
        self, *, tenant_id: str, tool_name: str, target_id: str
    ) -> Optional[SyntheticFault]:
        stmt = select(SyntheticFault).where(
            SyntheticFault.tenant_id == tenant_id,
            SyntheticFault.tool_name == tool_name,
            SyntheticFault.target_id == target_id,
        )
        return self._session.scalars(stmt).one_or_none()


class ActionProposalRepository:
    def __init__(self, session: Session) -> None:
        self._session = session

    def get_proposal(
        self, *, tenant_id: str, proposal_id: str
    ) -> Optional[ActionProposal]:
        stmt = select(ActionProposal).where(
            ActionProposal.tenant_id == tenant_id,
            ActionProposal.proposal_id == proposal_id,
        )
        return self._session.scalars(stmt).one_or_none()

    def create_proposal(
        self,
        *,
        tenant_id: str,
        proposal_id: str,
        action_type: str,
        target_id: str,
        target_version: int,
        payload_json: str,
        payload_hash: str,
        created_by: str = "agent",
    ) -> ActionProposal:
        proposal = ActionProposal(
            proposal_id=proposal_id,
            tenant_id=tenant_id,
            action_type=action_type,
            target_id=target_id,
            target_version=target_version,
            payload_json=payload_json,
            payload_hash=payload_hash,
            status="pending",
            created_by=created_by,
            created_at=datetime.utcnow(),
        )
        self._session.add(proposal)
        self._session.flush()
        return proposal

    def update_status(
        self, *, tenant_id: str, proposal_id: str, status: str
    ) -> Optional[ActionProposal]:
        proposal = self.get_proposal(tenant_id=tenant_id, proposal_id=proposal_id)
        if proposal:
            proposal.status = status
            self._session.flush()
        return proposal


class ApprovalRepository:
    def __init__(self, session: Session) -> None:
        self._session = session

    def get_latest_approval(
        self, *, tenant_id: str, proposal_id: str
    ) -> Optional[ApprovalRecord]:
        stmt = (
            select(ApprovalRecord)
            .where(
                ApprovalRecord.tenant_id == tenant_id,
                ApprovalRecord.proposal_id == proposal_id,
            )
            .order_by(ApprovalRecord.created_at.desc())
        )
        return self._session.scalars(stmt).first()

    def record_decision(
        self,
        *,
        tenant_id: str,
        approval_id: str,
        proposal_id: str,
        approver_id: str,
        decision: str,
        payload_hash: str,
    ) -> ApprovalRecord:
        record = ApprovalRecord(
            approval_id=approval_id,
            tenant_id=tenant_id,
            proposal_id=proposal_id,
            approver_id=approver_id,
            decision=decision,
            payload_hash=payload_hash,
            created_at=datetime.utcnow(),
        )
        self._session.add(record)
        self._session.flush()
        return record


class TicketRepository:
    def __init__(self, session: Session) -> None:
        self._session = session

    def get_ticket(
        self, *, tenant_id: str, ticket_id: str
    ) -> Optional[TicketRecord]:
        stmt = select(TicketRecord).where(
            TicketRecord.tenant_id == tenant_id,
            TicketRecord.ticket_id == ticket_id,
        )
        return self._session.scalars(stmt).one_or_none()

    def get_by_idempotency_key(
        self, *, tenant_id: str, idempotency_key: str
    ) -> Optional[TicketRecord]:
        stmt = select(TicketRecord).where(
            TicketRecord.tenant_id == tenant_id,
            TicketRecord.idempotency_key == idempotency_key,
        )
        return self._session.scalars(stmt).one_or_none()

    def create_ticket(
        self,
        *,
        tenant_id: str,
        ticket_id: str,
        proposal_id: Optional[str],
        title: str,
        description: str,
        priority: str,
        idempotency_key: str,
    ) -> TicketRecord:
        ticket = TicketRecord(
            ticket_id=ticket_id,
            tenant_id=tenant_id,
            proposal_id=proposal_id,
            title=title,
            description=description,
            priority=priority,
            status="open",
            idempotency_key=idempotency_key,
            created_at=datetime.utcnow(),
        )
        self._session.add(ticket)
        self._session.flush()
        return ticket


class PrerequisiteRepository:
    def __init__(self, session: Session) -> None:
        self._session = session

    def get_status(
        self, *, tenant_id: str, target_id: str, check_type: str
    ) -> Optional[PrerequisiteRecord]:
        stmt = select(PrerequisiteRecord).where(
            PrerequisiteRecord.tenant_id == tenant_id,
            PrerequisiteRecord.target_id == target_id,
            PrerequisiteRecord.check_type == check_type,
        )
        return self._session.scalars(stmt).one_or_none()

