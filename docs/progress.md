# progress.md —— 逐日进度与真实证据

规则：只记录**实际执行过**的命令与**实际输出**。未接通的环节明确标注，不用"预计""应该"填充。
没有证据的能力不写进"已完成"。

---

## D1 / 2026-09-28

**今天唯一目标**：不接模型，手工调用两个只读工具，稳定定位"IMP-104 为什么导入失败"，
并且证明跨租户访问不会泄漏。

**状态**：🟢 达标（限定范围内的 D1 已完成并验证）。

### 1. 环境记录（实测）

| 项 | 实测值 |
|---|---|
| Python | 3.10.11 |
| Node | v24.19.0 |
| Git | 2.55.0.windows.3 |
| SQLAlchemy | 2.0.52（系统 Python 已装，未建 venv） |
| pytest | 9.1.1 |

未安装/不可用项（已实测，不是猜测）：

- **无 PostgreSQL**：无 `psql`、无 postgres 服务。
- **Docker 守护进程未运行**：CLI 已装，`docker` 服务不可用。

### 2. 本次完成并验证的内容

| # | 内容 | 证据 |
|---|---|---|
| 1 | 合成业务模拟器（2 租户 / 4 用户 / 8 文件 / 8 导入任务 / 10 条日志 / 1 条故障注入） | `scripts/smoke.py` 输出的重置计数 |
| 2 | `get_import_job`：状态、错误码、file_id、版本、evidence ID | 见第 3 节实测返回 |
| 3 | `get_job_logs`：脱敏、限条数限长度、先校验任务可见性 | 同上；凭据值已脱敏 |
| 4 | 租户隔离由**仓储层强制**施加（每个方法 `tenant_id` keyword-only 必填） | `tests/test_tenant_isolation.py` |
| 5 | 身份在工具签名中**结构性排除** | `tests/test_read_tools.py::test_registry_hides_identity_parameters` |
| 6 | 合成故障为**数据表**注入，不是硬编码分支 | `synthetic_faults` 表 + D-006 |

### 3. 验证命令与真实输出

**命令 1**

```powershell
cd enterprise-agent-lab
python -m pytest -q
```

真实输出（尾行）：

```text
...............................                                          [100%]
```

31 项全部通过。执行耗时未计时（本文件不填估算值）。

**命令 2**

```powershell
python scripts/smoke.py
```

真实输出关键片段：

```text
[smoke] 合成数据已重置：{'tenants': 2, 'users': 4, 'files': 8, 'import_jobs': 8, 'job_logs': 10, 'synthetic_faults': 1}

[smoke] get_import_job(job_id='IMP-104')  →  期望 error_code=INVALID_DATE
{
  "ok": true,
  "data": {
    "job_id": "IMP-104", "status": "failed", "error_code": "INVALID_DATE",
    "file_id": "file-104", "filename": "roster_2026_09.csv",
    "product_version": "v1", "manual_version": "v1", "version": 3,
    "created_at": "2026-09-25T09:07:00"
  },
  "evidence": [
    {"id": "ev-job-IMP-104-v3",  "source_type": "job",  "source_id": "IMP-104",  "version": 3},
    {"id": "ev-file-file-104",   "source_type": "file", "source_id": "file-104", "version": 1}
  ],
  "error": null,
  "retryable": false
}
```

`get_job_logs(job_id='IMP-104')` → 5 条日志，其中根因行与脱敏行为：

```text
ERROR  第 3 行校验失败：字段 start_date 值 '2026-13-01' 不符合 YYYY-MM-DD   (log-104-03)
WARN   回调凭据 token=*** 已忽略（合成占位）                                  (log-104-04)
```

合成数据里该行的原始文本是 `token=syn-placeholder-not-a-real-secret`，
返回结果中**未出现该字符串**——脱敏在工具层生效，不是靠模型自觉。

`get_job_logs(job_id='IMP-107')`（故障注入 TIMEOUT）：

```json
{"ok": false, "data": null, "evidence": [],
 "error": {"code": "TIMEOUT", "message": "日志查询超时，未能取得日志内容"},
 "retryable": true}
```

关键点：**`data` 为 null、`evidence` 为空**，没有编造日志内容。

`get_import_job(job_id='IMP-999')`（不存在）：

```json
{"ok": false, "data": null, "evidence": [],
 "error": {"code": "NOT_FOUND_OR_FORBIDDEN", "message": "任务不存在或无权访问"}}
```

跨租户：以 `tenant_b` 身份查 `IMP-104`（真实属于 `tenant_a`）返回**完全相同**的错误结构与消息，
消息中不含 `IMP-104`、`tenant_a`、`INVALID_DATE` 等任何可用于确认"对象存在"的信息。

### 4. 真实 / 模拟 / 未接通 边界

| 类别 | 内容 |
|---|---|
| **真实** | 上述所有命令、输出、测试结果，均在本机实际执行获得 |
| **模拟** | 全部业务数据（租户、用户、文件、任务、日志、故障）均为合成；不含任何真实员工数据 |
| **未接通** | ① 模型 API：`MODEL_BASE_URL` / `MODEL_API_KEY` / `MODEL_NAME` 均为空，**未做过任何模型调用**；② 前端、API 服务、worker、检索、审批、工单均为未开始；③ PostgreSQL 未安装，D1 使用 SQLite |

**因此本阶段不得声明**：已接入模型、已实现单 Agent 工具循环、已实现多 Agent、
已实现审批与幂等、已部署。这些在 D2 及以后。

### 5. 失败与回滚点

| 失败现象 | 处置 |
|---|---|
| 合成数据被改坏 / 需要干净起点 | `python scripts/smoke.py` 会 `drop_all` → `create_all` → 重新灌入，即恢复初始状态 |
| 测试失败需定位 | `python -m pytest -v`（去掉 `-q` 看用例名）；`tests/conftest.py` 使用内存库，不污染文件库 |
| 误把种子指向真实库 | 内建防呆：`assert_synthetic_target()` 要求文件库路径含 `synthetic`，否则抛错拒绝执行（D-003） |

### 6. 成本与用量

| 项 | 实际值 |
|---|---|
| 模型调用次数 | **0** |
| token / 费用 | **0 元**（D1 不接模型） |
| 累计 API 支出 | 0 元 / 上限 200 元（连通性试验额度 30 元尚未动用） |

### 7. 未解决 / 阻塞

- 🔴 **D2 模型接入阻塞**：缺可用的模型 API Key。开工手册 §15 规定此时
  "目标收缩到两个工具与合成数据"，因此不伪造模型调用结果。
- 🟡 **`docs/decisions.md` D-002**：统一工具返回结构在两份文档中不一致
  （开工手册 §6.4 五项 vs FindYourself 方案 §3 八项），需与轨道 B 执行者协商出单一版本。
- 🟡 **SQLite ≠ RLS**：D1 的隔离结论只对"仓储层强制过滤"成立。
  切 Postgres 后必须复跑 `tests/test_tenant_isolation.py`（D-001 已定重审时点）。

### 8. 我能独立解释的一个设计选择

**为什么越权时返回"不存在或无权访问"，而不是 403？**
若返回 403，等于向调用方确认"该对象存在，只是你没权限"，
攻击者可用它枚举他租户的任务 ID。因此仓储层跨租户查询直接返回 `None`，
工具层统一映射为 `NOT_FOUND_OR_FORBIDDEN`，且消息里不带目标 ID；
真实原因仍记录在内部审计。参见 `docs/decisions.md` D-005。

### 9. 状态更新

D1 验收项全部闭环。后续在 D1 基础上扩展了工具集与编排骨架（见下一节），
其中**编排部分为确定性脚本，不调用模型**；真实模型闭环在 D2-real 一节，二者口径不同，不得混说。

---

## 编排骨架（脚本口径）/ 2026-09-28

**目标**：在 D1 工具层之上，搭出工具扩展、规程检索、多角色编排与质检退回的**骨架**，
以及同题 A/B/C 对照的**评测脚手架**与交互式工作台。

**状态**：🟢 骨架与回归通过（63 项测试通过，A/B/C 脚本对照数据已产出）。
🔴 **口径更正**：本节的 Agent 均为**确定性脚本**（正则 + 写死的工具序列），
**零次模型调用**；因此下表所有数字**不得**称为"模型实测"。
真实模型闭环见后面的 `D2-real` 一节。此更正对应 `docs/decisions.md` D-008。

### 1. 本次完成的核心系统产物

| 模块 | 实现内容 | 验证与证据 |
|---|---|---|
| **高级工具层** | `validate_csv`（校验数据类型与日期，隔离对抗文本）、`check_prerequisites`（核对前置幂等状态）、`create_ticket`（带不可变审批校验、参数 Hash 比对与幂等防重） | `tests/test_advanced_tools.py` 6项全通 |
| **知识与规程检索** | 12 篇企业运维手册（`packages/retrieval/runbooks.py`）+ 版本隔离检索（`v1` 规则隔离 `v2` 新规范） | `tests/test_retrieval.py` 3项全通 |
| **运行时核心** | `agent_core`：`TaskEnvelope` / `AgentResult` / `ReviewVerdict` 契约协议、`ExecutionBudget` 预算与轮次熔断、`ExecutionTracer` 审计轨迹与 SSE 推送、`ModelClient` 真实调用适配（D2 后改为**显式失败、不静默降级**） | `tests/test_agent_core.py` 7项全通 |
| **单智能体 OpsDesk** | 覆盖查询、追问（waiting_user）、越权拒绝、工具超时披露、草拟提案（waiting_approval） | `tests/test_single_agent.py` 5项全通 |
| **协同多智能体 IncidentOps** | Coordinator 动态路由 + Diagnosis 技术查证 + Remediation 方案草案 + Reviewer 质检门禁（支持 REWORK 自动补查循环，上限2轮；超限转人工） | `tests/test_multi_agent.py` 4项全通 |
| **同题 A/B/C 脚本对照** | 20 个冻结样例（`evals/holdout/frozen_cases.jsonl`），评估套件 `evals/runner.py` 与 Markdown 生成器（**脚本口径，不调模型**） | `evals/results/comparison_report.md` |
| **FastAPI 服务与工作台** | 8 个 REST/SSE 接口（`services/api/main.py`）+ 单页响应式工作台（`apps/web/index.html`） | `tests/test_api.py` 7项全通 |

### 2. 63 项工程测试实测记录

```text
$ python -m pytest --collect-only -q
tests/test_advanced_tools.py: 6
tests/test_agent_core.py: 7
tests/test_api.py: 7
tests/test_multi_agent.py: 4
tests/test_read_tools.py: 24
tests/test_retrieval.py: 3
tests/test_single_agent.py: 5
tests/test_tenant_isolation.py: 7

$ python -m pytest -q
...............................................................          [100%]
```

63 项全部通过。**注意**：这些测试不联网、不调模型，验证的是工具链与门禁逻辑，
不能作为"模型能力"的证据。

### 3. 20 个冻结样例 A/B/C 脚本对照指标（脚本输出，零模型调用）

⚠️ **以下数字是确定性脚本的输出**，不是模型实测。三个模式都不调用模型，
"耗时"是本地函数耗时。此处保留真实数值，但**口径必须按脚本读**。

```json
{
  "SingleAgent (A)": {
    "total_cases": 20,
    "completion_rate": 95.0,
    "fact_grounding_rate": 100.0,
    "unapproved_writes": 0,
    "avg_latency_ms": 4.43,
    "p95_latency_ms": 10.39
  },
  "FixedPipeline (B)": {
    "total_cases": 20,
    "completion_rate": 75.0,
    "fact_grounding_rate": 95.0,
    "unapproved_writes": 0,
    "avg_latency_ms": 1.22,
    "p95_latency_ms": 3.83
  },
  "MultiAgent (C)": {
    "total_cases": 20,
    "completion_rate": 100.0,
    "fact_grounding_rate": 100.0,
    "unapproved_writes": 0,
    "avg_latency_ms": 3.44,
    "p95_latency_ms": 8.16
  }
}
```

### 4. 关键技术取舍（脚本口径下的观察，不作为模型能力结论）

⚠️ 本节的"胜出/优势"仅在脚本口径下成立。真实模型下是否成立，须待多 Agent 接入真实模型后跑同题对照。

1. **为什么多智能体模式 C 能够达到 100% 完成率？**
   - 在证据缺口拦截上，针对超时重试场景（CASE-11 与 CASE-17），固定流水线会盲目发起重试导致事故风险，而 ReviewAgent 能够阻断无前提重试，发出 `REWORK` 指令强制核验幂等状态；
   - 在多门店混合故障排查（CASE-10）中，多 Agent 遵循规程输出独立双证据链，坚决阻断经验主义错误归并；
   - 权限拓扑隔离：DiagnosisAgent 永远接触不到写操作工具，从代码结构上消除越权。
2. **为什么单 Agent 模式 A 也具备价值？**
   - 针对单点明确问题，单 Agent 决策链路短，Token 消耗少、时延低；因此系统推荐由协调者动态路由：简单任务直接走单 Agent，复杂混合故障激活多 Agent 质检流程。

---

## D2-real / 2026-09-28 —— 真实模型闭环（agnes-3.0-flash）

**今天唯一目标**：接入真实模型（agnes-3.0-flash），跑通"模型决策 → 工具执行 → 观察回灌 → 结论"，
并记录**真实** token，不报任何未核对的金额。

**状态**：🟢 达标（限定范围：单 Agent、两个合成任务、只读工具集）。

### 1. 本次改了什么（对应 decisions.md D-008）

| 文件 | 变更 |
|---|---|
| `packages/agent_core/model_client.py` | **重写**：只做真实调用；无 Key / 调用失败一律抛 `ModelUnavailableError`；离线行为显式化为 `OfflineStubModelClient`（`usage_source="offline-stub"`、token 与费用恒 0）；单价未核对时 `pricing_verified=False` |
| `packages/agent_core/model_agent.py` | **新增** `ModelDrivenAgent`：模型返回 `tool_calls` → 闭包工具执行 → 观察回灌，全程计入预算与轨迹 |
| `scripts/d2_real_run.py` | **新增**：读 `.env` → 重建合成库 → 跑真实闭环 → 打印逐轮事件与 token → 写 `data/traces/` |
| `tests/test_agent_core.py` | 7 项；新增"无 Key 必须抛错""离线桩必须自我标识""模型不可用不得给结论"三条 |
| `.env.example` | 新增 `PRICE_PER_1M_INPUT_CNY` / `PRICE_PER_1M_OUTPUT_CNY`，说明**留空 = 单价未核对** |

> 为什么必须先做这件事：改前的 `ModelClient` 会在缺 Key/异常时**静默返回伪造结果 + 写死 token**
> （50/30）。这意味着任何"实测"都可能在零模型调用下产出看似真实的数字。
> 核查确认 `single_agent` / `multi_agent` / `evals` 三处**从未调用模型**——已在上一节更正口径。

### 2. 真实运行证据（命令 + 真实输出）

**命令**（在 `enterprise-agent-lab` 下；`.env` 已配好真实凭据）

```powershell
python scripts/d2_real_run.py
python scripts/d2_real_run.py "帮我看看 IMP-107 到底怎么了"
```

**IMP-104 逐轮轨迹（真实输出）**

```text
#1 [model_decision] 第 1 轮模型决策：请求调用 1 个工具   tokens: prompt=1071 completion=32 source=provider
#2 [tool_call_completed] 执行工具 get_import_job        ok=True
#3 [model_decision] 第 2 轮模型决策：请求调用 1 个工具   tokens: prompt=1313 completion=32 source=provider
#4 [tool_call_completed] 执行工具 get_job_logs          ok=True
#5 [model_decision] 第 3 轮模型决策：给出最终答复        tokens: prompt=2071 completion=550 source=provider
#6 [run_completed] 结束状态：answered
```

模型给出的结论引用了真实证据 ID：

```text
结论：IMP-104 失败原因是导入文件第 3 行 start_date 值 '2026-13-01' 月不合法（INVALID_DATE），
      且 retryable=false。
依据：ev-job-IMP-104-v3 / ev-log-log-104-03 / ev-log-log-104-05
建议：修正源数据后重新上传；明确声明"我这边没有写权限，无法代为创建工单"。
```

**IMP-107（合成故障注入 TIMEOUT）关键片段**

```text
#4 [tool_call_completed] 执行工具 get_job_logs         ok=False   → 工具超时
#5 [tool_call_completed] 执行工具 check_prerequisites   ok=True
#6 [tool_call_completed] 执行工具 search_runbooks       ok=True
#9 [model_decision] 第 4 轮模型决策：给出最终答复
```

模型对日志缺失的处理（**未编造**）：

```text
2. 日志获取失败：日志查询接口返回 TIMEOUT，未能取得脱敏日志内容，无法进一步看到具体哪一步耗时。
   （无日志证据，如实说明）
```

### 3. 真实用量与费用边界

| 序 | 运行 | 模型调用次数 | prompt tokens | completion tokens | trace 文件 |
|---|---|---|---|---|---|
| 1 | 连通性四点检查 | 2 | 363 | 34 | 无（一次性脚本） |
| 2 | IMP-104 首跑 | 3 | 4455 | 614 | 被第 4 步覆盖，数值取自当次控制台输出 |
| 3 | IMP-107 | 4 | 6874 | 728 | `data/traces/d2-imp107_trace.jsonl` |
| 4 | IMP-104 重跑（为留下可核验 trace） | 4 | 7775 | 806 | `data/traces/d2-imp104_trace.jsonl` |
| — | **合计** | **13** | **19467** | **2182** | 总计 21649 tokens |

费用：**不报金额**。`PRICE_PER_1M_*_CNY` 未配置 → `pricing_verified=False`，
费用恒为 0，也**不计入**预算消耗。累计 API 支出：**未折算** / 上限 200 元（连通性额度 30 元未单独计费）。

### 4. 真实 / 脚本 / 未接通 边界

| 类别 | 内容 |
|---|---|
| **真实** | 13 次对 `agnes-3.0-flash` 的调用、12 次工具执行（全部命中合成库）、上述 token 与 trace 文件，均在本机实际执行获得 |
| **脚本** | `packages/single_agent`、`packages/multi_agent`、`evals/` 的 A/B/C 对照：**零次模型调用** |
| **未接通** | ① 多 Agent 尚未接真实模型；② 真实认证（`resolve_principal` 仍是占位，D-004）；③ 单价未核对；④ PostgreSQL 未安装（D-001）；⑤ 媒体/检索等其余能力 |

**因此本阶段不得声明**：多智能体已由模型驱动、A/B/C 是模型能力对比、已部署、已修好真实系统。
可声明的是：**单 Agent 的真实模型工具闭环已跑通，且有真实 token 与轨迹留痕**。

### 5. 失败与回滚点

| 失败现象 | 处置 / 观测 |
|---|---|
| 模型 Key 失效或网络不通 | `ModelUnavailableError`，脚本以非零码退出，**不产出结论**（已由单测覆盖） |
| 模型调用失控消耗 | `ExecutionBudget` 熔断（8 轮 / 10 次工具 / 2 元），返回 `budget_exceeded` |
| 需要干净起点 | 重跑 `python scripts/d2_real_run.py`，内部会 `drop_all → create_all → 重新灌入` |
| 想不联网复现整条链路 | 单测走 `OfflineStubModelClient`，`pytest -q` 秒级完成 |

### 6. 我能独立解释的一个设计选择

**为什么把"离线保底"从默认行为改成必须显式声明？**
因为静默保底会让报告里出现"看起来是实测、实际是本地伪造"的数字，
一旦写进简历就无法自证。改成"缺 Key 就抛错 + 离线桩自我标识（usage_source=offline-stub、
token 与费用为 0）"后，"有没有真的调用模型"变成**可从数据字段直接核对**的事实，
而不是靠人声明。参见 `docs/decisions.md` D-008。

