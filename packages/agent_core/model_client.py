"""OpenAI 兼容的模型调用客户端。

真实调用走 `ModelClient`；没有 key 或调用失败时直接抛异常，不伪造结果。
离线演示/回归用 `OfflineStubModelClient`，会显式标记 `usage_source="offline-stub"`。
"""

from __future__ import annotations

import json
import os
import re
import time
from typing import Any, Optional


class ModelUnavailableError(RuntimeError):
    """模型不可用：无 Key、网络失败或上游返回错误。绝不返回编造结果代替。"""


def _env_price(name: str) -> Optional[float]:
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return None
    try:
        return float(raw)
    except ValueError:
        return None


class ModelClient:
    """真实模型客户端。所有网络失败都显式抛出，不返回任何替代内容。"""

    def __init__(
        self,
        base_url: Optional[str] = None,
        api_key: Optional[str] = None,
        model_name: Optional[str] = None,
        price_per_1m_input_cny: Optional[float] = None,
        price_per_1m_output_cny: Optional[float] = None,
        timeout: float = 60.0,
    ) -> None:
        self.base_url = base_url or os.environ.get("MODEL_BASE_URL") or ""
        self.api_key = (
            api_key
            if api_key is not None
            else (os.environ.get("MODEL_API_KEY") or "")
        )
        self.model_name = model_name or os.environ.get("MODEL_NAME") or ""

        self.price_per_1m_input_cny = (
            price_per_1m_input_cny
            if price_per_1m_input_cny is not None
            else _env_price("PRICE_PER_1M_INPUT_CNY")
        )
        self.price_per_1m_output_cny = (
            price_per_1m_output_cny
            if price_per_1m_output_cny is not None
            else _env_price("PRICE_PER_1M_OUTPUT_CNY")
        )

        self.timeout = timeout
        self._client: Any = None
        if self.api_key and self.base_url and self.model_name:
            try:
                import openai

                self._client = openai.OpenAI(
                    base_url=self.base_url,
                    api_key=self.api_key,
                    timeout=timeout,
                )
            except Exception as exc:  # 依赖缺失或参数非法
                raise ModelUnavailableError(
                    f"模型客户端初始化失败（{type(exc).__name__}），未做任何替代调用"
                ) from exc

    # --- 计费 ---------------------------------------------------------------

    @property
    def pricing_verified(self) -> bool:
        """单价是否已人工核对。未核对时费用一律不计，避免编造金额。"""
        return (
            self.price_per_1m_input_cny is not None
            and self.price_per_1m_output_cny is not None
        )

    def calculate_cost(self, prompt_tokens: int, completion_tokens: int) -> float:
        if not self.pricing_verified:
            return 0.0
        assert self.price_per_1m_input_cny is not None
        assert self.price_per_1m_output_cny is not None
        cost = (prompt_tokens / 1_000_000.0) * self.price_per_1m_input_cny
        cost += (completion_tokens / 1_000_000.0) * self.price_per_1m_output_cny
        return round(cost, 6)

    # --- 调用 ---------------------------------------------------------------

    def chat_completion(
        self,
        messages: list[dict[str, Any]],
        tools: Optional[list[dict[str, Any]]] = None,
        temperature: float = 0.1,
        max_tokens: int = 1024,
    ) -> dict[str, Any]:
        """发起带工具支持的模型对话。

        返回：
            {
                "content": str,
                "tool_calls": list[{"id","name","arguments"}],
                "prompt_tokens": int,
                "completion_tokens": int,
                "cost_cny": float,
                "model": str,
                "usage_source": "provider",
                "pricing_verified": bool,
            }

        失败一律抛 `ModelUnavailableError`，不返回编造的工具调用或用量。
        """
        if self._client is None:
            raise ModelUnavailableError(
                "模型未配置完整（需要 MODEL_BASE_URL / MODEL_API_KEY / MODEL_NAME），"
                "不伪造模型结果"
            )

        call_kwargs: dict[str, Any] = {
            "model": self.model_name,
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
        }
        if tools:
            call_kwargs["tools"] = [self._to_openai_tool(t) for t in tools]
            call_kwargs["tool_choice"] = "auto"

        try:
            resp = self._client.chat.completions.create(**call_kwargs)
        except Exception as exc:
            raise ModelUnavailableError(
                f"模型调用失败（{type(exc).__name__}）：{_short(str(exc))}"
            ) from exc

        choice = resp.choices[0].message
        tool_calls: list[dict[str, Any]] = []
        for tc in choice.tool_calls or []:
            try:
                args = json.loads(tc.function.arguments)
            except Exception:
                args = {}
            tool_calls.append(
                {"id": tc.id, "name": tc.function.name, "arguments": args}
            )

        usage = resp.usage
        prompt_tokens = int(getattr(usage, "prompt_tokens", 0) or 0) if usage else 0
        completion_tokens = (
            int(getattr(usage, "completion_tokens", 0) or 0) if usage else 0
        )

        return {
            "content": choice.content or "",
            "tool_calls": tool_calls,
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
            "cost_cny": self.calculate_cost(prompt_tokens, completion_tokens),
            "model": getattr(resp, "model", self.model_name),
            "usage_source": "provider",
            "pricing_verified": self.pricing_verified,
        }

    @staticmethod
    def _to_openai_tool(tool: dict[str, Any]) -> dict[str, Any]:
        if tool.get("type") == "function":
            return tool
        return {
            "type": "function",
            "function": {
                "name": tool.get("name", ""),
                "description": tool.get("description", ""),
                "parameters": tool.get("parameters", {"type": "object", "properties": {}}),
            },
        }


class OfflineStubModelClient(ModelClient):
    """显式离线桩：只在明确需要"不联网也能确定性复现"时使用。

    绝不自动接管真实调用；其返回不带真实 token 与费用，
    并通过 `usage_source="offline-stub"` 让上层可识别并拒绝把它计入实测统计。
    """

    def __init__(self, model_name: str = "offline-stub") -> None:
        super().__init__(base_url="offline://stub", api_key="", model_name=model_name)

    def chat_completion(
        self,
        messages: list[dict[str, Any]],
        tools: Optional[list[dict[str, Any]]] = None,
        temperature: float = 0.1,
        max_tokens: int = 1024,
    ) -> dict[str, Any]:
        result = self._heuristic(messages)
        result.update(
            {
                "prompt_tokens": 0,
                "completion_tokens": 0,
                "cost_cny": 0.0,
                "model": self.model_name,
                "usage_source": "offline-stub",
                "pricing_verified": False,
            }
        )
        return result

    def _heuristic(self, messages: list[dict[str, Any]]) -> dict[str, Any]:
        last_user = next(
            (str(m.get("content", "")) for m in reversed(messages) if m.get("role") == "user"),
            "",
        )
        job_match = re.search(r"IMP-[A-Za-z0-9]+", last_user)
        target_job = job_match.group(0) if job_match else None

        last_is_tool = bool(messages) and messages[-1].get("role") == "tool"
        if last_is_tool:
            return {"content": "（离线桩）已根据工具返回汇总，不做真实推理。", "tool_calls": []}
        if target_job:
            return {
                "content": "",
                "tool_calls": [
                    {
                        "id": f"tc-stub-{int(time.time() * 1000)}",
                        "name": "get_import_job",
                        "arguments": {"job_id": target_job},
                    }
                ],
            }
        return {"content": "（离线桩）未识别到任务 ID。", "tool_calls": []}


def _short(text: str, limit: int = 300) -> str:
    cleaned = " ".join(text.split())
    return cleaned[:limit]