"""FastAPI 接口层测试。"""

import pytest
from fastapi.testclient import TestClient
from services.api.main import app

client = TestClient(app)


def test_api_index_html():
    response = client.get("/")
    assert response.status_code == 200
    assert "IncidentOps" in response.text


def test_api_create_run_and_query():
    # 模拟操作员发起排查
    response = client.post(
        "/runs",
        headers={"x-user-id": "u-a-operator"},
        json={"input": "请查 IMP-104 为什么失败", "mode": "multi"},
    )
    assert response.status_code == 200
    data = response.json()
    run_id = data["run_id"]
    assert run_id.startswith("run-")

    # 查询任务详情
    status_resp = client.get(f"/runs/{run_id}", headers={"x-user-id": "u-a-operator"})
    assert status_resp.status_code == 200
    status_data = status_resp.json()
    assert status_data["tenant_id"] == "tenant_a"


def test_api_evals_summary():
    response = client.get("/evals/summary")
    assert response.status_code == 200
    data = response.json()
    assert "SingleAgent (A)" in data["summary"]
    assert "MultiAgent (C)" in data["summary"]


def test_openai_models_endpoint():
    response = client.get("/v1/models")
    assert response.status_code == 200
    data = response.json()
    assert data["object"] == "list"
    model_ids = [m["id"] for m in data["data"]]
    assert "incidentops-multi-agent" in model_ids
    assert "opsdesk-single-agent" in model_ids


def test_openai_chat_completions_sync():
    # 测试标准 OpenAI ChatCompletion 接口（非流式）
    response = client.post(
        "/v1/chat/completions",
        headers={"Authorization": "Bearer u-a-operator"},
        json={
            "model": "incidentops-multi-agent",
            "messages": [{"role": "user", "content": "请诊断任务 IMP-104 故障原因"}],
            "stream": False,
        },
    )
    assert response.status_code == 200
    data = response.json()
    assert data["object"] == "chat.completion"
    assert len(data["choices"]) > 0
    assert "IMP-104" in data["choices"][0]["message"]["content"]
    assert "usage" in data


def test_openai_chat_completions_stream():
    # 测试标准 OpenAI ChatCompletion 流式接口 (SSE)
    response = client.post(
        "/v1/chat/completions",
        headers={"Authorization": "Bearer u-a-operator"},
        json={
            "model": "incidentops-multi-agent",
            "messages": [{"role": "user", "content": "请诊断任务 IMP-104 故障原因"}],
            "stream": True,
        },
    )
    assert response.status_code == 200
    assert "text/event-stream" in response.headers["content-type"]
    text = response.text
    assert "chat.completion.chunk" in text
    assert "[DONE]" in text


def test_list_runbooks():
    response = client.get("/runbooks", headers={"x-user-id": "u-a-operator"})
    assert response.status_code == 200
    data = response.json()
    assert "runbooks" in data
    assert len(data["runbooks"]) > 0
    doc_ids = [r["doc_id"] for r in data["runbooks"]]
    assert "RB-CSV-01" in doc_ids


def test_evidence_detail_endpoints():
    # 1. 验证日志证据详情读取
    res_log = client.get("/evidence/ev-log-log-104-01", headers={"x-user-id": "u-a-operator"})
    assert res_log.status_code == 200
    log_json = res_log.json()
    assert log_json["type"] == "log"
    assert "log-104-01" in log_json["data"]["log_id"]

    # 2. 验证文件元数据读取
    res_file = client.get("/evidence/ev-file-file-104", headers={"x-user-id": "u-a-operator"})
    assert res_file.status_code == 200
    assert res_file.json()["type"] == "file"

    # 3. 验证规程实体读取
    res_rb = client.get("/evidence/ev-runbook-chunk-err-invalid-date", headers={"x-user-id": "u-a-operator"})
    assert res_rb.status_code == 200
    assert res_rb.json()["type"] == "runbook"

    # 4. 验证跨租户越权读取阻断（tenant_b 的证据对 tenant_a 返回 404）
    res_cross = client.get("/evidence/ev-job-IMP-B01-v1", headers={"x-user-id": "u-a-operator"})
    assert res_cross.status_code == 404


def test_create_run_engine_modes():
    # 测试确定性规则基线模式
    res_det = client.post(
        "/runs",
        headers={"x-user-id": "u-a-operator"},
        json={"input": "请查 IMP-104 为什么失败", "mode": "multi", "engine_mode": "deterministic"},
    )
    assert res_det.status_code == 200
    run_det_id = res_det.json()["run_id"]
    stat_det = client.get(f"/runs/{run_det_id}", headers={"x-user-id": "u-a-operator"}).json()
    assert stat_det["engine_mode"] == "deterministic"

    # 测试大模型驱动模式
    res_llm = client.post(
        "/runs",
        headers={"x-user-id": "u-a-operator"},
        json={"input": "请查 IMP-104 为什么失败", "mode": "multi", "engine_mode": "llm"},
    )
    assert res_llm.status_code == 200
    run_llm_id = res_llm.json()["run_id"]
    stat_llm = client.get(f"/runs/{run_llm_id}", headers={"x-user-id": "u-a-operator"}).json()
    assert stat_llm["engine_mode"] == "llm"



