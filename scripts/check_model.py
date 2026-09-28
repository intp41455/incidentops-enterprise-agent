"""连通性四点检查：地址、模型名、工具调用、用量返回。

依据《AI-Agent项目开工手册》§3：选 API 只检查四点——
能访问、支持工具调用、能返回用量、价格能查清。
本脚本只做前三点（价格需人工核对供应商页面）。

用法（在 enterprise-agent-lab 下，先把 .env 的 MODEL_* 导入环境）：

    Get-Content .env | ForEach-Object { if ($_ -match '^\\s*([A-Z_]+)\\s*=\\s*(.+)$') {
        Set-Item -Path ("env:" + $matches[1]) -Value $matches[2].Trim() } }
    python scripts/check_model.py

本脚本**不含任何凭据**：Key 只从环境变量读取，不写入仓库、不写入代码。
"""

import json
import os
import urllib.error
import urllib.request

BASE_URL = os.environ["MODEL_BASE_URL"].rstrip("/")
API_KEY = os.environ["MODEL_API_KEY"]
MODEL = os.environ["MODEL_NAME"]

# AGENTS.md 3.4：系统代理可能指向死端口，显式绕过本地
os.environ.setdefault("NO_PROXY", "localhost,127.0.0.1,::1")


def post(path: str, payload: dict) -> tuple[int, dict]:
    req = urllib.request.Request(
        BASE_URL + path,
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {API_KEY}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        return exc.code, {"_raw": body[:800]}


def main() -> int:
    for var in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy"):
        if os.environ.get(var):
            print(f"[env] {var} = {os.environ[var]}")

    print(f"[1/3] 地址与模型可达性：{BASE_URL} / {MODEL}")
    status, body = post(
        "/chat/completions",
        {
            "model": MODEL,
            "messages": [{"role": "user", "content": "只回复两个字：可达"}],
            "max_tokens": 16,
        },
    )
    print(f"  HTTP {status}")
    if status != 200:
        print(f"  返回：{json.dumps(body, ensure_ascii=False)[:800]}")
        return 1
    print(f"  内容：{body['choices'][0]['message'].get('content')!r}")
    print(f"  usage：{body.get('usage')}")

    print(f"[2/3] 工具调用能力：投递一个 tool schema，看是否返回 tool_calls")
    status, body = post(
        "/chat/completions",
        {
            "model": MODEL,
            "messages": [
                {"role": "user", "content": "查一下导入任务 IMP-104 的状态。"}
            ],
            "tools": [
                {
                    "type": "function",
                    "function": {
                        "name": "get_import_job",
                        "description": "查询导入任务的状态与错误码",
                        "parameters": {
                            "type": "object",
                            "properties": {"job_id": {"type": "string"}},
                            "required": ["job_id"],
                        },
                    },
                }
            ],
            "tool_choice": "auto",
            "max_tokens": 128,
        },
    )
    print(f"  HTTP {status}")
    if status != 200:
        print(f"  返回：{json.dumps(body, ensure_ascii=False)[:800]}")
        return 1
    msg = body["choices"][0]["message"]
    calls = msg.get("tool_calls") or []
    print(f"  finish_reason：{body['choices'][0].get('finish_reason')}")
    print(f"  tool_calls：{json.dumps(calls, ensure_ascii=False)}")
    print(f"  usage：{body.get('usage')}")

    print(f"[3/3] 用量字段是否可用于计费核算")
    usage = body.get("usage") or {}
    ok = {"prompt_tokens", "completion_tokens", "total_tokens"} <= set(usage)
    print(f"  三件套齐全：{ok}  {usage}")

    print("\n结论：" + ("三点均通过；价格需人工核对供应商页面" if calls and ok else "存在未通过项，见上方输出"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())