# IncidentOps / OpsDesk

一个用来练手的企业运维 Agent 原型，处理 CSV 导入失败排查、规程核对和受控工单创建。

在线演示：[https://incidentops.pages.dev/](https://incidentops.pages.dev/)

> 说明：这是个人学习和面试准备项目，数据全部是虚构的。核心想验证的是“多 Agent 分角色 + 审批门禁”在企业故障排查场景里能不能比单 Agent 更稳。

---

## 背景

之前看过一些单体 ReAct Agent 的 demo，发现复杂场景下容易出几个问题：

1. 工具一多，模型容易选错工具或编参数。
2. 日志、CSV 数据塞进上下文后，后面推理质量下降。
3. 多任务一起失败时，模型喜欢经验主义合并归因。
4. 涉及写操作（比如创建工单、重试任务）时，没有审查闭环，容易带病执行。

所以这个项目尝试用四个角色来分工：Coordinator 负责派活和汇总，Diagnosis 只做只读调查，Remediation 负责查规程和出方案，Review 做独立复核。写操作必须先形成提案，人工审批后才执行。

---

## 架构

```
User -> CoordinatorAgent
         │
         ├──> DiagnosisAgent (get_import_job / get_job_logs / validate_csv)
         │
         ├──> RemediationAgent (search_runbooks / check_prerequisites)
         │
         └──> ReviewAgent (PASS / REWORK / ESCALATE)
                       │
                       ▼
              ActionProposal (pending)
                       │
                       ▼
              Human Approval
                       │
                       ▼
              create_ticket (idempotent)
```

- **CoordinatorAgent**：解析意图、拆任务、汇总事实、处理 Review 的退回。
- **DiagnosisAgent**：只读调查，收集任务状态、日志、CSV 校验结果。
- **RemediationAgent**：按错误码检索规程，超时场景必须先确认幂等状态。
- **ReviewAgent**：检查证据链是否闭环，缺证据就打回补查（最多 2 轮），冲突严重则转人工。

---

## 本地跑起来

```bash
git clone https://github.com/intp41455/incidentops-enterprise-agent.git
cd incidentops-enterprise-agent

pip install -e .
pip install pytest pytest-asyncio anyio respx mock uvicorn

# 跑测试
python -m pytest -q

# 启动服务
python -m uvicorn services.api.main:app --host 127.0.0.1 --port 8000
```

打开 `http://127.0.0.1:8000/` 即可看到工作台。

---

## 接口示例

```bash
# OpenAI 兼容接口，SSE 流式输出
curl http://localhost:8000/v1/chat/completions \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer u-a-operator" \
  -d '{
    "model": "incidentops-multi-agent",
    "messages": [{"role": "user", "content": "IMP-104 导入失败，帮我查原因；能处理的话帮我处理。"}],
    "stream": true
  }'
```

---

## 当前局限（面试时会被问到，提前写清楚）

1. **身份认证是占位实现**：现在从 Header 里直接读 `user_id`，生产上需要接 JWT/SSO。
2. **规程库是硬编码**：`runbooks.py` 里是手写的 8 篇文档片段，没有上向量检索/RAG。
3. **默认 Agent 是确定性规则基线**：`CoordinatorAgent` / `OpsDeskAgent` 目前不走真实 LLM；真实 LLM 驱动在 `agent_core/model_agent.py` 和 `multi_agent/llm_coordinator.py` 里，需要配模型 key 才能跑。
4. **前端是单文件静态页**：演示用，没有复杂状态管理。
5. **数据库默认 SQLite**：需要切 PostgreSQL + RLS 才能进生产。

详见 [TODO.md](./TODO.md)。

---

## 测试

```bash
python -m pytest -q
```

68 个用例，覆盖工具层、租户隔离、审批流程、多 Agent REWORK、API 层。

---

## 技术栈

- Python 3.10+
- FastAPI + Uvicorn
- SQLAlchemy 2.0
- OpenAI 兼容模型接口
- pytest

---

## 许可证

Apache License 2.0
