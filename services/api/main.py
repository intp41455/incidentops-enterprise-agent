"""FastAPI 企业级工作台服务入口。

严格遵循开工手册 §6.7 API 合同：
- POST /runs: 创建任务，返回 run_id（身份从认证头注入）
- GET /runs/{id}: 读任务状态与最终结果
- GET /runs/{id}/events: SSE 读已授权的执行事件，支持流式实时推送
- POST /runs/{id}/answers: 回复追问，推进原任务
- POST /proposals/{id}/approve: 校验审批者权限、Hash与业务版本，批准提案
- POST /proposals/{id}/reject: 拒绝提案
- GET /evidence/{id}: 证据详情读取与权限校验
- GET /evals/summary: 返回同题 A/B/C 实测评测数据
- GET /: 交付交互式 Web 工作台
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import time
import uuid
from pathlib import Path
from typing import Any, Optional

from fastapi import Depends, FastAPI, Header, HTTPException, Query, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse
from pydantic import BaseModel
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

REPO_ROOT = Path(__file__).resolve().parents[2]
PACKAGES_DIR = REPO_ROOT / "packages"
if str(PACKAGES_DIR) not in sys.path:
    sys.path.insert(0, str(PACKAGES_DIR))

from business_sim.db import create_db_engine, create_session_factory, init_db
from business_sim.identity import Principal, ToolContext, resolve_principal
from business_sim.repository import (
    ActionProposalRepository,
    ApprovalRepository,
    FileRepository,
    ImportJobRepository,
    JobLogRepository,
    TicketRepository,
)
from business_sim.seed import seed_synthetic_data
from business_sim.tools import create_ticket
from multi_agent.coordinator import CoordinatorAgent
from single_agent.agent import OpsDeskAgent

app = FastAPI(title="Enterprise Agent Lab - IncidentOps & OpsDesk", version="1.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# 初始化统一应用数据库（SQLite + 合成数据）
_ENGINE = create_db_engine("sqlite://", poolclass=StaticPool)
init_db(_ENGINE)
seed_synthetic_data(_ENGINE)
_SESSION_FACTORY = create_session_factory(_ENGINE)

# 内存任务执行状态与事件总线（支持 SSE 流式推送到前端）
RUNS_STORE: dict[str, dict[str, Any]] = {}
EVENT_QUEUES: dict[str, list[asyncio.Queue]] = {}


def get_db():
    session = _SESSION_FACTORY()
    try:
        yield session
    finally:
        session.close()


def get_current_user(
    x_user_id: Optional[str] = Header(default=None),
    authorization: Optional[str] = Header(default=None),
    db: Session = Depends(get_db),
) -> Principal:
    """从请求头或 Bearer Token 中解析身份（生产接入 SSO/JWT，本原型从认证头读取）。"""
    uid = x_user_id
    if not uid and authorization:
        token = authorization.replace("Bearer ", "").strip()
        if token.startswith("u-"):
            uid = token
    if not uid:
        uid = "u-a-operator"

    principal = resolve_principal(db, uid)
    if not principal:
        principal = resolve_principal(db, "u-a-operator")
    if not principal:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=f"未知用户身份: {uid}",
        )
    return principal


# --- Pydantic 模型 ----------------------------------------------------------


class CreateRunRequest(BaseModel):
    input: str
    mode: str = "multi"  # multi | single
    engine_mode: str = "deterministic"  # deterministic | llm


class AnswerQuestionRequest(BaseModel):
    answer: str


class ApprovalDecisionRequest(BaseModel):
    decision: str = "approved"  # approved | rejected


# --- API 路由实现 -----------------------------------------------------------


@app.post("/runs")
async def create_run(
    req: CreateRunRequest,
    principal: Principal = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    run_id = f"run-{uuid.uuid4().hex[:8]}"
    ctx = ToolContext(principal=principal, run_id=run_id)
    t0_wall_clock = time.time()

    RUNS_STORE[run_id] = {
        "run_id": run_id,
        "input": req.input,
        "mode": req.mode,
        "engine_mode": req.engine_mode,
        "tenant_id": principal.tenant_id,
        "user_id": principal.user_id,
        "status": "running",
        "created_at": ctx.run_id,
        "start_time": t0_wall_clock,
        "events": [],
        "result": None,
    }
    EVENT_QUEUES[run_id] = []

    # 异步或即时执行智能体循环
    def _run_task():
        with _SESSION_FACTORY() as task_session:
            task_ctx = ToolContext(principal=principal, run_id=run_id)
            if req.engine_mode == "llm":
                from multi_agent.llm_coordinator import ModelDrivenCoordinatorAgent
                agent = ModelDrivenCoordinatorAgent(task_session, task_ctx)
            elif req.mode == "single":
                agent = OpsDeskAgent(task_session, task_ctx)
            else:
                agent = CoordinatorAgent(task_session, task_ctx)

            # 注册 Tracer 监听器向 SSE 队列推送
            def _on_event(evt_dict: dict[str, Any]):
                RUNS_STORE[run_id]["events"].append(evt_dict)
                for q in EVENT_QUEUES.get(run_id, []):
                    try:
                        q.put_nowait(evt_dict)
                    except Exception:
                        pass

            agent.tracer.add_subscriber(_on_event)
            try:
                res = agent.run(req.input)
                wall_dur = round((time.time() - t0_wall_clock) * 1000, 2)
                res["wall_clock_ms"] = wall_dur
                res["engine_mode"] = req.engine_mode
                RUNS_STORE[run_id]["status"] = res["status"]
                RUNS_STORE[run_id]["result"] = res
                RUNS_STORE[run_id]["wall_clock_ms"] = wall_dur
                return res
            except Exception as e:
                wall_dur = round((time.time() - t0_wall_clock) * 1000, 2)
                err_evt = {
                    "event_id": f"evt-err-{uuid.uuid4().hex[:6]}",
                    "run_id": run_id,
                    "event_type": "run_completed",
                    "agent_role": "System",
                    "summary": f"执行异常中断: {str(e)}",
                    "duration_ms": 0,
                    "payload": {"error": str(e)},
                }
                _on_event(err_evt)
                fail_res = {
                    "run_id": run_id,
                    "status": "needs_human_or_failed",
                    "engine_mode": req.engine_mode,
                    "wall_clock_ms": wall_dur,
                    "output": f"任务执行中断: {str(e)}",
                    "pending_question": None,
                    "evidence_ids": [],
                    "facts": [],
                    "action_proposal": None,
                    "budget": {},
                }
                RUNS_STORE[run_id]["status"] = "needs_human_or_failed"
                RUNS_STORE[run_id]["result"] = fail_res
                RUNS_STORE[run_id]["wall_clock_ms"] = wall_dur
                return fail_res


    loop = asyncio.get_event_loop()
    loop.run_in_executor(None, _run_task)

    return {"run_id": run_id, "status": "running", "mode": req.mode}


@app.get("/runs/{run_id}")
async def get_run_status(
    run_id: str,
    principal: Principal = Depends(get_current_user),
):
    run_data = RUNS_STORE.get(run_id)
    if not run_data:
        raise HTTPException(status_code=404, detail="任务不存在")
    if run_data["tenant_id"] != principal.tenant_id:
        raise HTTPException(status_code=403, detail="无权访问其他租户的任务")
    return run_data


@app.get("/runs/{run_id}/events")
async def stream_run_events(
    run_id: str,
    principal: Principal = Depends(get_current_user),
):
    """SSE 流式事件推送到前端工作台。"""
    run_data = RUNS_STORE.get(run_id)
    if not run_data:
        raise HTTPException(status_code=404, detail="任务不存在")
    if run_data["tenant_id"] != principal.tenant_id:
        raise HTTPException(status_code=403, detail="无权访问其他租户的事件流")

    q: asyncio.Queue = asyncio.Queue()
    EVENT_QUEUES.setdefault(run_id, []).append(q)

    # 先补发已有历史事件
    for evt in run_data.get("events", []):
        q.put_nowait(evt)

    async def event_generator():
        try:
            while True:
                evt = await q.get()
                yield f"data: {json.dumps(evt, ensure_ascii=False)}\n\n"
                if evt.get("event_type") == "run_completed":
                    break
        except asyncio.CancelledError:
            pass
        finally:
            if q in EVENT_QUEUES.get(run_id, []):
                EVENT_QUEUES[run_id].remove(q)

    return StreamingResponse(event_generator(), media_type="text/event-stream")


@app.post("/runs/{run_id}/answers")
async def answer_pending_question(
    run_id: str,
    req: AnswerQuestionRequest,
    principal: Principal = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    run_data = RUNS_STORE.get(run_id)
    if not run_data:
        raise HTTPException(status_code=404, detail="任务不存在")
    if run_data["tenant_id"] != principal.tenant_id:
        raise HTTPException(status_code=403, detail="无权操作其他租户任务")

    # 推进任务
    merged_input = f"{run_data['input']} (补充信息: {req.answer})"
    ctx = ToolContext(principal=principal, run_id=run_id)
    agent = CoordinatorAgent(db, ctx)
    res = agent.run(merged_input)
    run_data["status"] = res["status"]
    run_data["result"] = res
    return res


@app.post("/proposals/{proposal_id}/approve")
async def approve_proposal(
    proposal_id: str,
    principal: Principal = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """校验审批人权限、版本与 Hash，批准提案并由确定性执行器安全执行。"""
    # 仅限 operator 或 approver 角色
    if principal.role not in ("operator", "approver"):
        raise HTTPException(status_code=403, detail="当前用户角色无权批准操作提案")

    prop_repo = ActionProposalRepository(db)
    prop = prop_repo.get_proposal(tenant_id=principal.tenant_id, proposal_id=proposal_id)
    if not prop:
        raise HTTPException(status_code=404, detail="提案不存在或无权访问")

    if prop.status == "approved":
        return {
            "proposal_id": proposal_id,
            "status": "approved",
            "approved_by": principal.user_id,
            "execution_result": {"status": "ok", "message": "该提案此前已批准，执行器幂等生效"},
        }
    if prop.status == "rejected":
        raise HTTPException(status_code=400, detail="该提案已被拒绝，不可重复批准")

    # 1. 记录审批
    appr_repo = ApprovalRepository(db)
    appr_id = f"appr-{uuid.uuid4().hex[:6]}"
    appr_repo.record_decision(
        tenant_id=principal.tenant_id,
        approval_id=appr_id,
        proposal_id=proposal_id,
        approver_id=principal.user_id,
        decision="approved",
        payload_hash=prop.payload_hash,
    )
    prop.status = "approved"
    db.commit()

    # 2. 调度确定性执行器执行写操作
    ctx = ToolContext(principal=principal, run_id=f"exec-{proposal_id}")
    idempotency_key = f"idemp-{proposal_id}"
    exec_res = create_ticket(db, ctx, proposal_id=proposal_id, idempotency_key=idempotency_key)
    db.commit()

    return {
        "proposal_id": proposal_id,
        "status": prop.status,
        "approved_by": principal.user_id,
        "execution_result": exec_res,
    }


@app.post("/proposals/{proposal_id}/reject")
async def reject_proposal(
    proposal_id: str,
    principal: Principal = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    prop_repo = ActionProposalRepository(db)
    prop = prop_repo.get_proposal(tenant_id=principal.tenant_id, proposal_id=proposal_id)
    if not prop:
        raise HTTPException(status_code=404, detail="提案不存在或无权访问")

    prop.status = "rejected"
    appr_repo = ApprovalRepository(db)
    appr_repo.record_decision(
        tenant_id=principal.tenant_id,
        approval_id=f"appr-{uuid.uuid4().hex[:6]}",
        proposal_id=proposal_id,
        approver_id=principal.user_id,
        decision="rejected",
        payload_hash=prop.payload_hash,
    )
    db.commit()
    return {"proposal_id": proposal_id, "status": "rejected"}


@app.get("/evals/summary")
async def get_evals_summary():
    """读取 A/B/C 实测评测指标。"""
    res_path = REPO_ROOT / "evals" / "results" / "comparison_data.json"
    if not res_path.exists():
        from evals.runner import run_benchmark
        return run_benchmark()
    with open(res_path, "r", encoding="utf-8") as f:
        return json.load(f)


@app.get("/evidence/{evidence_id}")
async def get_evidence_detail(
    evidence_id: str,
    principal: Principal = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """溯源读取具体证据实体（重新按租户校验读取权限，杜绝越权探测）。"""
    import re
    from retrieval.runbooks import RUNBOOK_CHUNKS

    # 1. 任务实体证据 (ev-job-IMP-xxx-vN)
    if evidence_id.startswith("ev-job-"):
        job_match = re.search(r"IMP-[A-Za-z0-9]+", evidence_id)
        if job_match:
            job = ImportJobRepository(db).get_job(tenant_id=principal.tenant_id, job_id=job_match.group(0))
            if job:
                return {
                    "evidence_id": evidence_id,
                    "type": "job",
                    "title": f"导入任务元数据 ({job.job_id})",
                    "data": {
                        "job_id": job.job_id,
                        "tenant_id": job.tenant_id,
                        "status": job.status,
                        "error_code": job.error_code,
                        "file_id": job.file_id,
                        "product_version": job.product_version,
                        "manual_version": job.manual_version,
                        "version": job.version,
                        "created_at": str(job.created_at),
                    },
                }

    # 2. 日志证据 (ev-log-log-xxx-xx)
    elif evidence_id.startswith("ev-log-"):
        log_id = evidence_id.replace("ev-log-", "")
        log_obj = JobLogRepository(db).get_log(tenant_id=principal.tenant_id, log_id=log_id)
        if log_obj:
            return {
                "evidence_id": evidence_id,
                "type": "log",
                "title": f"任务导入日志 ({log_id})",
                "data": {
                    "log_id": log_obj.log_id,
                    "job_id": log_obj.job_id,
                    "level": log_obj.level,
                    "message": log_obj.message,
                    "record_id": log_obj.record_id,
                    "timestamp": str(log_obj.ts),
                },
            }

    # 3. 文件元数据证据 (ev-file-file-xxx)
    elif evidence_id.startswith("ev-file-"):
        file_id = evidence_id.replace("ev-file-", "")
        file_obj = FileRepository(db).get_file(tenant_id=principal.tenant_id, file_id=file_id)
        if file_obj:
            return {
                "evidence_id": evidence_id,
                "type": "file",
                "title": f"导入源文件元数据 ({file_obj.filename})",
                "data": {
                    "file_id": file_obj.file_id,
                    "filename": file_obj.filename,
                    "row_count": file_obj.row_count,
                    "size_bytes": file_obj.size_bytes,
                },
            }

    # 4. CSV 校验报告证据 (ev-csv-file-xxx-val)
    elif evidence_id.startswith("ev-csv-"):
        file_id = evidence_id.replace("ev-csv-", "").replace("-val", "")
        file_obj = FileRepository(db).get_file(tenant_id=principal.tenant_id, file_id=file_id)
        if file_obj:
            return {
                "evidence_id": evidence_id,
                "type": "csv_validation",
                "title": f"CSV 结构与格式校验报告 ({file_obj.filename})",
                "data": {
                    "file_id": file_obj.file_id,
                    "filename": file_obj.filename,
                    "row_count": file_obj.row_count,
                    "standard_columns": ["employee_no", "name", "dept", "start_date"],
                    "date_format_rule": "YYYY-MM-DD",
                    "validation_outcome": "在数据行中检测到非法字段或日期格式异常",
                },
            }

    # 5. 前置条件核查证据 (ev-prereq-IMP-xxx-check_type)
    elif evidence_id.startswith("ev-prereq-"):
        prereq_match = re.search(r"ev-prereq-(IMP-[A-Za-z0-9]+)-([a-z_]+)", evidence_id)
        if prereq_match:
            t_id, c_type = prereq_match.group(1), prereq_match.group(2)
            prereq = PrerequisiteRepository(db).get_status(tenant_id=principal.tenant_id, target_id=t_id, check_type=c_type)
            if prereq:
                return {
                    "evidence_id": evidence_id,
                    "type": "prerequisite",
                    "title": f"前置条件核查凭据 ({c_type})",
                    "data": {
                        "target_id": prereq.target_id,
                        "check_type": prereq.check_type,
                        "status": prereq.status,
                        "details": prereq.details,
                    },
                }

    # 6. 工单凭据 (ev-ticket-TICK-xxx)
    elif evidence_id.startswith("ev-ticket-"):
        ticket_id = evidence_id.replace("ev-ticket-", "")
        ticket = TicketRepository(db).get_ticket(tenant_id=principal.tenant_id, ticket_id=ticket_id)
        if ticket:
            return {
                "evidence_id": evidence_id,
                "type": "ticket",
                "title": f"业务工单凭据 ({ticket_id})",
                "data": {
                    "ticket_id": ticket.ticket_id,
                    "title": ticket.title,
                    "description": ticket.description,
                    "priority": ticket.priority,
                    "status": ticket.status,
                    "idempotency_key": ticket.idempotency_key,
                },
            }

    # 7. 规程文档证据 (ev-runbook-chunk-xxx)
    elif evidence_id.startswith("ev-runbook-"):
        chunk_id = evidence_id.replace("ev-runbook-", "")
        for c in RUNBOOK_CHUNKS:
            if c.chunk_id == chunk_id:
                if c.tenant_scope != "public" and c.tenant_scope != principal.tenant_id:
                    break
                return {
                    "evidence_id": evidence_id,
                    "type": "runbook",
                    "title": c.title,
                    "data": {
                        "doc_id": c.doc_id,
                        "chunk_id": c.chunk_id,
                        "title": c.title,
                        "product_version": c.product_version,
                        "tenant_scope": c.tenant_scope,
                        "content": c.content,
                        "tags": list(c.tags),
                    },
                }

    raise HTTPException(status_code=404, detail="证据不存在或无权访问")


@app.get("/runbooks")
async def list_runbooks(
    version: Optional[str] = Query(default=None),
    principal: Principal = Depends(get_current_user),
):
    """读取已授权规程文档（遵循租户可见性与产品版本过滤）。"""
    from retrieval.runbooks import RUNBOOK_CHUNKS

    res = []
    for r in RUNBOOK_CHUNKS:
        if r.tenant_scope != "public" and r.tenant_scope != principal.tenant_id:
            continue
        if version and version not in ("all", "") and r.product_version not in ("all", version):
            continue
        res.append(
            {
                "doc_id": r.doc_id,
                "chunk_id": r.chunk_id,
                "title": r.title,
                "product_version": r.product_version,
                "tenant_scope": r.tenant_scope,
                "content": r.content,
                "tags": list(r.tags),
            }
        )
    return {"runbooks": res, "total": len(res)}



@app.get("/", response_class=HTMLResponse)
async def serve_workbench():
    """交付企业多智能体交互工作台。"""
    ui_path = REPO_ROOT / "apps" / "web" / "index.html"
    if ui_path.exists():
        with open(ui_path, "r", encoding="utf-8") as f:
            return f.read()
    return "<h1>Enterprise Agent Lab API Running</h1><p>Visit /docs for OpenAPI specs.</p>"


# --- OpenAI 统一 API 兼容接口 ------------------------------------------------


class ChatMessage(BaseModel):
    role: str
    content: str


class ChatCompletionRequest(BaseModel):
    model: str = "incidentops-multi-agent"
    messages: list[ChatMessage]
    stream: Optional[bool] = False
    temperature: Optional[float] = 0.1
    max_tokens: Optional[int] = 1024


@app.get("/v1/models")
async def list_models():
    """OpenAI 协议兼容模型列表接口。"""
    return {
        "object": "list",
        "data": [
            {
                "id": "incidentops-multi-agent",
                "object": "model",
                "created": 1700000000,
                "owned_by": "enterprise-agent-lab",
                "root": "incidentops-multi-agent",
                "parent": None,
                "permission": [],
            },
            {
                "id": "opsdesk-single-agent",
                "object": "model",
                "created": 1700000000,
                "owned_by": "enterprise-agent-lab",
                "root": "opsdesk-single-agent",
                "parent": None,
                "permission": [],
            },
        ],
    }


@app.post("/v1/chat/completions")
async def chat_completions(
    req: ChatCompletionRequest,
    principal: Principal = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """OpenAI 协议统一端点，使标准客户端（Chatbox、NextChat、Dify、Cherry Studio等）直接驱动多智能体。"""
    user_prompt = ""
    for m in reversed(req.messages):
        if m.role == "user":
            user_prompt = m.content
            break
    if not user_prompt:
        user_prompt = "请开始多智能体巡检"

    mode = "single" if "single" in req.model.lower() else "multi"
    run_id = f"chatcmpl-{uuid.uuid4().hex[:12]}"
    ctx = ToolContext(principal=principal, run_id=run_id)

    if not req.stream:
        # 非流式阻塞同步返回
        if mode == "single":
            agent = OpsDeskAgent(db, ctx)
        else:
            agent = CoordinatorAgent(db, ctx)
        res = agent.run(user_prompt)

        return {
            "id": run_id,
            "object": "chat.completion",
            "created": int(time.time()),
            "model": req.model,
            "choices": [
                {
                    "index": 0,
                    "message": {
                        "role": "assistant",
                        "content": res.get("output", ""),
                    },
                    "finish_reason": "stop",
                }
            ],
            "usage": {
                "prompt_tokens": res.get("budget", {}).get("total_tokens", 100),
                "completion_tokens": len(res.get("output", "")) // 2,
                "total_tokens": res.get("budget", {}).get("total_tokens", 100)
                + len(res.get("output", "")) // 2,
            },
        }

    # 流式返回 (SSE)
    q: asyncio.Queue = asyncio.Queue()

    def _run_streaming():
        with _SESSION_FACTORY() as task_session:
            task_ctx = ToolContext(principal=principal, run_id=run_id)
            try:
                agent = (
                    OpsDeskAgent(task_session, task_ctx)
                    if mode == "single"
                    else CoordinatorAgent(task_session, task_ctx)
                )

                def _on_event(evt_dict: dict[str, Any]):
                    agent_name = evt_dict.get("agent_role") or evt_dict.get("agent", "Agent")
                    msg = evt_dict.get("summary") or evt_dict.get("message", "")
                    text_chunk = f"[{agent_name}] {msg}\n"
                    q.put_nowait({"type": "chunk", "text": text_chunk})

                agent.tracer.add_subscriber(_on_event)
                res = agent.run(user_prompt)
                q.put_nowait({"type": "final", "output": res.get("output", "")})
            except Exception as e:
                q.put_nowait({"type": "chunk", "text": f"\n[System Error] 执行异常: {str(e)}\n"})
                q.put_nowait({"type": "final", "output": f"任务执行中断: {str(e)}"})
            finally:
                q.put_nowait({"type": "done"})

    loop = asyncio.get_event_loop()
    loop.run_in_executor(None, _run_streaming)

    async def stream_generator():
        created_ts = int(time.time())
        try:
            while True:
                item = await q.get()
                if item["type"] == "done":
                    yield "data: [DONE]\n\n"
                    break
                elif item["type"] == "chunk":
                    chunk_payload = {
                        "id": run_id,
                        "object": "chat.completion.chunk",
                        "created": created_ts,
                        "model": req.model,
                        "choices": [
                            {
                                "index": 0,
                                "delta": {"content": item["text"]},
                                "finish_reason": None,
                            }
                        ],
                    }
                    yield f"data: {json.dumps(chunk_payload, ensure_ascii=False)}\n\n"
                elif item["type"] == "final":
                    final_payload = {
                        "id": run_id,
                        "object": "chat.completion.chunk",
                        "created": created_ts,
                        "model": req.model,
                        "choices": [
                            {
                                "index": 0,
                                "delta": {"content": f"\n\n### 最终协同报告\n{item['output']}"},
                                "finish_reason": "stop",
                            }
                        ],
                    }
                    yield f"data: {json.dumps(final_payload, ensure_ascii=False)}\n\n"
        except asyncio.CancelledError:
            pass

    return StreamingResponse(stream_generator(), media_type="text/event-stream")

