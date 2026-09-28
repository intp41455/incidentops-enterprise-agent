# IncidentOps / OpsDesk

企业运维 Agent 原型，处理 CSV 导入失败排查与受控工单创建。

## 本地运行

```bash
pip install -e .
pip install pytest pytest-asyncio anyio respx mock uvicorn
python -m pytest -q
python -m uvicorn services.api.main:app --host 127.0.0.1 --port 8000
```

## 技术栈

- Python 3.10+
- FastAPI + Uvicorn
- SQLAlchemy 2.0
- OpenAI 兼容模型接口
