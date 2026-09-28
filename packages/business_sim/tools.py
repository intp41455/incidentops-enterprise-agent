"""统一工具层：`get_import_job` 与 `get_job_logs`。

统一返回结构（开工手册 §6.4）：

    {ok, data, evidence, error, retryable}

错误码至少包含：`NOT_FOUND_OR_FORBIDDEN` / `INVALID_ARGUMENT` / `TIMEOUT` /
`STALE_VERSION` / `BUDGET_EXCEEDED`。

**权限靠结构保证，不靠提示词**：工具函数签名为 `tool(session, ctx, ...)`，
`ctx` 由服务端构造；对模型暴露的入口由 `build_tool_registry()` 生成，
其可调用签名只含业务参数（`job_id`、`limit`），租户与角色在闭包里绑定。
见 docs/decisions.md D-004、D-005。
"""

from __future__ import annotations

import json
import re
from typing import Any, Callable, Optional

from sqlalchemy.orm import Session

from .identity import ToolContext
from .models import FileRecord, ImportJob, JobLog
from .repository import (
    ActionProposalRepository,
    ApprovalRepository,
    FileRepository,
    ImportJobRepository,
    JobLogRepository,
    PrerequisiteRepository,
    SyntheticFaultRepository,
    TicketRepository,
)

TOOL_GET_IMPORT_JOB = "get_import_job"
TOOL_GET_JOB_LOGS = "get_job_logs"

DEFAULT_LOG_LIMIT = 50
MAX_LOG_LIMIT = 100
MAX_LOG_MESSAGE_CHARS = 500

ERR_NOT_FOUND_OR_FORBIDDEN = "NOT_FOUND_OR_FORBIDDEN"
ERR_INVALID_ARGUMENT = "INVALID_ARGUMENT"
ERR_TIMEOUT = "TIMEOUT"
ERR_STALE_VERSION = "STALE_VERSION"
ERR_BUDGET_EXCEEDED = "BUDGET_EXCEEDED"

_JOB_ID_PATTERN = re.compile(r"^IMP-[A-Za-z0-9]{2,12}$")

# 日志脱敏：值部分整体替换，保留键名便于排查
_SECRET_PATTERNS = (
    re.compile(r"(?i)\b(token|api[_-]?key|password|passwd|secret)\s*[=:]\s*\S+"),
)
_REDACTION_REPLACEMENT = r"\1=***"


# --- 结果构造 ---------------------------------------------------------------


def _ok(data: Any, evidence: Optional[list[dict[str, Any]]] = None) -> dict[str, Any]:
    return {
        "ok": True,
        "data": data,
        "evidence": list(evidence or []),
        "error": None,
        "retryable": False,
    }


def _err(code: str, message: str, *, retryable: bool = False) -> dict[str, Any]:
    return {
        "ok": False,
        "data": None,
        "evidence": [],
        "error": {"code": code, "message": message},
        "retryable": retryable,
    }


# --- 脱敏与证据 -------------------------------------------------------------


def redact_secrets(text: str) -> str:
    masked = text
    for pattern in _SECRET_PATTERNS:
        masked = pattern.sub(_REDACTION_REPLACEMENT, masked)
    return masked


def _job_evidence(job: ImportJob) -> dict[str, Any]:
    return {
        "id": f"ev-job-{job.job_id}-v{job.version}",
        "source_type": "job",
        "source_id": job.job_id,
        "version": job.version,
    }


def _file_evidence(record: FileRecord) -> dict[str, Any]:
    return {
        "id": f"ev-file-{record.file_id}",
        "source_type": "file",
        "source_id": record.file_id,
        "version": 1,
    }


def _log_evidence(row: JobLog) -> dict[str, Any]:
    return {
        "id": f"ev-log-{row.log_id}",
        "source_type": "log",
        "source_id": row.log_id,
        "version": 1,
    }


# --- 参数校验 ---------------------------------------------------------------


def _job_id_problem(job_id: Any) -> Optional[str]:
    if not isinstance(job_id, str):
        return "job_id 必须是字符串"
    if not _JOB_ID_PATTERN.match(job_id.strip()):
        return "job_id 格式无效，应形如 IMP-104"
    return None


def _limit_problem(limit: Any) -> Optional[str]:
    if isinstance(limit, bool) or not isinstance(limit, int):
        return "limit 必须是整数"
    if not 1 <= limit <= MAX_LOG_LIMIT:
        return f"limit 必须在 1..{MAX_LOG_LIMIT} 之间"
    return None


# --- 工具实现 ---------------------------------------------------------------


def get_import_job(session: Session, ctx: ToolContext, job_id: str) -> dict[str, Any]:
    """查询本租户某个导入任务的状态与错误码。租户来自 ctx，模型不可指定。"""
    problem = _job_id_problem(job_id)
    if problem is not None:
        return _err(ERR_INVALID_ARGUMENT, problem)

    fault = SyntheticFaultRepository(session).find(
        tenant_id=ctx.tenant_id, tool_name=TOOL_GET_IMPORT_JOB, target_id=job_id
    )
    if fault is not None:
        return _err(ERR_TIMEOUT, "查询任务状态超时，未取得结果", retryable=True)

    job = ImportJobRepository(session).get_job(tenant_id=ctx.tenant_id, job_id=job_id)
    if job is None:
        # 不区分"不存在"与"无权访问"，错误消息不含 job_id（D-005）
        return _err(ERR_NOT_FOUND_OR_FORBIDDEN, "任务不存在或无权访问")

    evidence: list[dict[str, Any]] = [_job_evidence(job)]
    record = FileRepository(session).get_file(
        tenant_id=ctx.tenant_id, file_id=job.file_id
    )
    if record is not None:
        evidence.append(_file_evidence(record))

    data = {
        "job_id": job.job_id,
        "status": job.status,
        "error_code": job.error_code,
        "file_id": job.file_id,
        "filename": record.filename if record is not None else None,
        "product_version": job.product_version,
        "manual_version": job.manual_version,
        "version": job.version,
        "created_at": job.created_at.isoformat(),
    }
    return _ok(data, evidence)


def get_job_logs(
    session: Session,
    ctx: ToolContext,
    job_id: str,
    limit: int = DEFAULT_LOG_LIMIT,
) -> dict[str, Any]:
    """读取本租户某个导入任务的脱敏日志。"""
    problem = _job_id_problem(job_id)
    if problem is not None:
        return _err(ERR_INVALID_ARGUMENT, problem)

    problem = _limit_problem(limit)
    if problem is not None:
        return _err(ERR_INVALID_ARGUMENT, problem)

    fault = SyntheticFaultRepository(session).find(
        tenant_id=ctx.tenant_id, tool_name=TOOL_GET_JOB_LOGS, target_id=job_id
    )
    if fault is not None:
        # 明确失败，绝不返回编造的日志内容
        return _err(ERR_TIMEOUT, "日志查询超时，未能取得日志内容", retryable=True)

    job = ImportJobRepository(session).get_job(tenant_id=ctx.tenant_id, job_id=job_id)
    if job is None:
        return _err(ERR_NOT_FOUND_OR_FORBIDDEN, "任务不存在或无权访问")

    log_repo = JobLogRepository(session)
    rows = log_repo.list_logs(tenant_id=ctx.tenant_id, job_id=job_id, limit=limit)
    total = log_repo.count_logs(tenant_id=ctx.tenant_id, job_id=job_id)

    data = {
        "job_id": job_id,
        "count": len(rows),
        "total": total,
        "truncated": total > len(rows),
        "logs": [
            {
                "log_id": row.log_id,
                "ts": row.ts.isoformat(),
                "level": row.level,
                "message": redact_secrets(row.message)[:MAX_LOG_MESSAGE_CHARS],
                "record_id": row.record_id,
            }
            for row in rows
        ],
    }
    evidence = [_job_evidence(job)] + [_log_evidence(row) for row in rows]
    return _ok(data, evidence)


# --- validate_csv -----------------------------------------------------------

_DATE_PATTERN = re.compile(r"^\d{4}-(0[1-9]|1[0-2])-(0[1-9]|[12]\d|3[01])$")


def validate_csv(session: Session, ctx: ToolContext, file_id: str) -> dict[str, Any]:
    """校验已上传的合成 CSV 文件的字段与格式规范。"""
    if not isinstance(file_id, str) or not file_id.strip():
        return _err(ERR_INVALID_ARGUMENT, "file_id 必须是非空字符串")

    fault = SyntheticFaultRepository(session).find(
        tenant_id=ctx.tenant_id, tool_name="validate_csv", target_id=file_id
    )
    if fault is not None:
        return _err(ERR_TIMEOUT, "CSV 文件读取校验超时", retryable=True)

    file_record = FileRepository(session).get_file(
        tenant_id=ctx.tenant_id, file_id=file_id
    )
    if file_record is None:
        return _err(ERR_NOT_FOUND_OR_FORBIDDEN, "文件不存在或无权访问")

    rows_data: list[dict[str, Any]] = []
    if file_record.sample_rows_json:
        try:
            rows_data = json.loads(file_record.sample_rows_json)
        except Exception:
            rows_data = []

    errors: list[dict[str, Any]] = []
    # 规则校验：检查必须字段与日期格式
    for row_idx, row in enumerate(rows_data, start=1):
        # 1. 必填字段检查 (支持 emp_id 或 employee_no)
        has_emp = ("emp_id" in row and str(row["emp_id"]).strip()) or (
            "employee_no" in row and str(row["employee_no"]).strip()
        )
        if not has_emp:
            errors.append(
                {
                    "row": row_idx,
                    "field": "emp_id",
                    "error_type": "MISSING_REQUIRED_FIELD",
                    "message": f"第 {row_idx} 行缺少员工编号 (emp_id/employee_no)",
                    "raw_value": None,
                }
            )
        for req_field in ("name", "start_date"):
            if req_field not in row or str(row[req_field]).strip() == "":
                errors.append(
                    {
                        "row": row_idx,
                        "field": req_field,
                        "error_type": "MISSING_REQUIRED_FIELD",
                        "message": f"第 {row_idx} 行缺少必填字段 '{req_field}'",
                        "raw_value": None,
                    }
                )
        # 2. 日期格式校验 (YYYY-MM-DD)
        start_date = str(row.get("start_date", "")).strip()
        if start_date and not _DATE_PATTERN.match(start_date):
            errors.append(
                {
                    "row": row_idx,
                    "field": "start_date",
                    "error_type": "INVALID_DATE_FORMAT",
                    "message": f"第 {row_idx} 行 start_date 值 '{start_date}' 不符合 YYYY-MM-DD 规范",
                    "raw_value": start_date,
                }
            )


    data = {
        "file_id": file_record.file_id,
        "filename": file_record.filename,
        "total_rows": file_record.row_count,
        "valid": len(errors) == 0,
        "error_count": len(errors),
        "errors": errors,
    }
    evidence = [
        {
            "id": f"ev-csv-{file_record.file_id}-val",
            "source_type": "csv_validation",
            "source_id": file_record.file_id,
            "version": 1,
        }
    ]
    return _ok(data, evidence)


# --- check_prerequisites ----------------------------------------------------


def check_prerequisites(
    session: Session, ctx: ToolContext, target_id: str, check_type: str
) -> dict[str, Any]:
    """核对重试或修复操作的前置依赖条件（如上游服务状态、幂等写入状态）。"""
    if not isinstance(target_id, str) or not target_id.strip():
        return _err(ERR_INVALID_ARGUMENT, "target_id 必须是非空字符串")
    if not isinstance(check_type, str) or not check_type.strip():
        return _err(ERR_INVALID_ARGUMENT, "check_type 必须是非空字符串")

    repo = PrerequisiteRepository(session)
    record = repo.get_status(
        tenant_id=ctx.tenant_id, target_id=target_id, check_type=check_type
    )

    if record is None:
        # 默认正常满足
        status = "passed"
        details = "默认前置条件满足"
    else:
        status = record.status
        details = record.details

    data = {
        "target_id": target_id,
        "check_type": check_type,
        "status": status,
        "details": details,
        "passed": status == "passed",
    }
    evidence = [
        {
            "id": f"ev-prereq-{target_id}-{check_type}",
            "source_type": "prerequisite",
            "source_id": target_id,
            "version": 1,
        }
    ]
    return _ok(data, evidence)


# --- get_ticket_status & create_ticket --------------------------------------


def get_ticket_status(session: Session, ctx: ToolContext, ticket_id: str) -> dict[str, Any]:
    """查询工单当前状态与版本。"""
    if not isinstance(ticket_id, str) or not ticket_id.strip():
        return _err(ERR_INVALID_ARGUMENT, "ticket_id 必须是非空字符串")

    ticket = TicketRepository(session).get_ticket(
        tenant_id=ctx.tenant_id, ticket_id=ticket_id
    )
    if ticket is None:
        return _err(ERR_NOT_FOUND_OR_FORBIDDEN, "工单不存在或无权访问")

    data = {
        "ticket_id": ticket.ticket_id,
        "title": ticket.title,
        "status": ticket.status,
        "priority": ticket.priority,
        "created_at": ticket.created_at.isoformat(),
    }
    evidence = [
        {
            "id": f"ev-ticket-{ticket.ticket_id}",
            "source_type": "ticket",
            "source_id": ticket.ticket_id,
            "version": 1,
        }
    ]
    return _ok(data, evidence)


def create_ticket(
    session: Session,
    ctx: ToolContext,
    proposal_id: str,
    idempotency_key: str,
) -> dict[str, Any]:
    """执行受控写动作：根据已批准的提案创建支持工单。具备审批状态校验、参数 Hash 比对与幂等防护。"""
    if not isinstance(proposal_id, str) or not proposal_id.strip():
        return _err(ERR_INVALID_ARGUMENT, "proposal_id 必须是非空字符串")
    if not isinstance(idempotency_key, str) or not idempotency_key.strip():
        return _err(ERR_INVALID_ARGUMENT, "idempotency_key 必须是非空字符串")

    # 1. 幂等性先验检查（网络重试/重复点击直接返回历史结果）
    ticket_repo = TicketRepository(session)
    existing_ticket = ticket_repo.get_by_idempotency_key(
        tenant_id=ctx.tenant_id, idempotency_key=idempotency_key
    )
    if existing_ticket is not None:
        return _ok(
            {
                "ticket_id": existing_ticket.ticket_id,
                "status": existing_ticket.status,
                "title": existing_ticket.title,
                "idempotency_result": "replay",
                "message": "幂等命中：已存在相同幂等键的工单，直接返回原结果，未重复创建",
            },
            evidence=[
                {
                    "id": f"ev-ticket-{existing_ticket.ticket_id}",
                    "source_type": "ticket",
                    "source_id": existing_ticket.ticket_id,
                    "version": 1,
                }
            ],
        )

    # 2. 查询提案
    proposal = ActionProposalRepository(session).get_proposal(
        tenant_id=ctx.tenant_id, proposal_id=proposal_id
    )
    if proposal is None:
        return _err(ERR_NOT_FOUND_OR_FORBIDDEN, "提案不存在或无权访问")

    # 3. 状态检查：必须已获审批
    if proposal.status != "approved":
        return _err(
            "UNAPPROVED_ACTION",
            f"提案当前状态为 '{proposal.status}'，尚未经过人工批准，系统拒绝执行写操作",
        )

    # 4. 审批有效性与 Hash 比对
    approval = ApprovalRepository(session).get_latest_approval(
        tenant_id=ctx.tenant_id, proposal_id=proposal_id
    )
    if approval is None or approval.decision != "approved":
        return _err("UNAPPROVED_ACTION", "未检索到针对该提案的有效批准记录")

    if approval.payload_hash != proposal.payload_hash:
        return _err(
            ERR_STALE_VERSION,
            "提案参数已在审批后被修改（Hash不一致），原审批已失效，请重新发起审批",
        )

    # 5. 执行创建

    try:
        payload = json.loads(proposal.payload_json)
    except Exception:
        payload = {}

    import uuid
    ticket_id = f"TICK-{proposal.target_id.replace('IMP-', '')}-{uuid.uuid4().hex[:4].upper()}"
    title = payload.get("title", f"支持工单: {proposal.target_id} 导入异常处理")
    description = payload.get("description", "由智能体排查后经人工审批创建的支持工单")
    priority = payload.get("priority", "P2")

    ticket = ticket_repo.create_ticket(
        tenant_id=ctx.tenant_id,
        ticket_id=ticket_id,
        proposal_id=proposal_id,
        title=title,
        description=description,
        priority=priority,
        idempotency_key=idempotency_key,
    )
    proposal.status = "executed"
    session.flush()

    return _ok(
        {
            "ticket_id": ticket.ticket_id,
            "status": ticket.status,
            "title": ticket.title,
            "idempotency_result": "created",
            "message": "工单创建成功",
        },
        evidence=[
            {
                "id": f"ev-ticket-{ticket.ticket_id}",
                "source_type": "ticket",
                "source_id": ticket.ticket_id,
                "version": 1,
            }
        ],
    )


# --- 对模型暴露的入口 -------------------------------------------------------


def build_tool_registry(
    session: Session, ctx: ToolContext
) -> dict[str, Callable[..., dict[str, Any]]]:
    """生成只含业务参数的工具入口。

    `session` 与 `ctx` 在此闭包绑定，模型无法传参覆盖 tenant_id / user_id / role。
    """

    def _get_import_job(job_id: str) -> dict[str, Any]:
        return get_import_job(session, ctx, job_id)

    def _get_job_logs(
        job_id: str, limit: int = DEFAULT_LOG_LIMIT
    ) -> dict[str, Any]:
        return get_job_logs(session, ctx, job_id, limit)

    def _validate_csv(file_id: str) -> dict[str, Any]:
        return validate_csv(session, ctx, file_id)

    def _check_prerequisites(target_id: str, check_type: str) -> dict[str, Any]:
        return check_prerequisites(session, ctx, target_id, check_type)

    def _get_ticket_status(ticket_id: str) -> dict[str, Any]:
        return get_ticket_status(session, ctx, ticket_id)

    def _search_runbooks(query: str, product_version: str = "v1") -> dict[str, Any]:
        from retrieval.engine import search_runbooks
        return search_runbooks(session, ctx, query=query, product_version=product_version)

    return {
        TOOL_GET_IMPORT_JOB: _get_import_job,
        TOOL_GET_JOB_LOGS: _get_job_logs,
        "validate_csv": _validate_csv,
        "check_prerequisites": _check_prerequisites,
        "get_ticket_status": _get_ticket_status,
        "search_runbooks": _search_runbooks,
    }


# 工具输入输出类型说明。
TOOL_SCHEMAS: dict[str, dict[str, Any]] = {
    TOOL_GET_IMPORT_JOB: {
        "name": TOOL_GET_IMPORT_JOB,
        "description": "查询本租户某个导入任务的状态与错误码。",
        "parameters": {
            "type": "object",
            "properties": {"job_id": {"type": "string", "description": "形如 IMP-104"}},
            "required": ["job_id"],
            "additionalProperties": False,
        },
    },
    TOOL_GET_JOB_LOGS: {
        "name": TOOL_GET_JOB_LOGS,
        "description": "读取本租户某个导入任务的脱敏日志，用于定位失败原因。",
        "parameters": {
            "type": "object",
            "properties": {
                "job_id": {"type": "string", "description": "形如 IMP-104"},
                "limit": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": MAX_LOG_LIMIT,
                    "default": DEFAULT_LOG_LIMIT,
                },
            },
            "required": ["job_id"],
            "additionalProperties": False,
        },
    },
    "validate_csv": {
        "name": "validate_csv",
        "description": "校验已上传 CSV 文件的格式、字段完整性与数据行规范，获得具体行号与错误项。",
        "parameters": {
            "type": "object",
            "properties": {"file_id": {"type": "string", "description": "文件ID，形如 file-104"}},
            "required": ["file_id"],
            "additionalProperties": False,
        },
    },
    "check_prerequisites": {
        "name": "check_prerequisites",
        "description": "检查任务是否满足重试或修复的前置依赖条件（如幂等写入状态、上游服务健康度）。",
        "parameters": {
            "type": "object",
            "properties": {
                "target_id": {"type": "string", "description": "目标任务ID，形如 IMP-203"},
                "check_type": {"type": "string", "description": "检查项类型，如 idempotency_status 或 upstream_health"},
            },
            "required": ["target_id", "check_type"],
            "additionalProperties": False,
        },
    },
    "get_ticket_status": {
        "name": "get_ticket_status",
        "description": "查询工单当前状态与版本。",
        "parameters": {
            "type": "object",
            "properties": {"ticket_id": {"type": "string", "description": "工单ID，形如 TICK-104-ABCD"}},
            "required": ["ticket_id"],
            "additionalProperties": False,
        },
    },
    "search_runbooks": {
        "name": "search_runbooks",
        "description": "检索导入规范与运维手册，了解错误码处理原则、重试前置条件与工单政策。",
        "parameters": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "检索关键词，如 'INVALID_DATE' 或 '超时重试'"},
                "product_version": {"type": "string", "description": "产品版本，如 'v1' 或 'v2'", "default": "v1"},
            },
            "required": ["query"],
            "additionalProperties": False,
        },
    },
}


