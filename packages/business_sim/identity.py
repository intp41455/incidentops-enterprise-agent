"""服务端身份与工具上下文。

**核心约束**：租户、用户、角色永远由服务端解析，绝不来自模型输出或客户端请求体。
见 docs/decisions.md D-004。
"""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy.orm import Session

from .repository import UserRepository

# 允许的角色。D1 未实现权限判定，仅做身份携带；D12–D13 补角色过滤。
ROLES = ("viewer", "operator", "approver")


@dataclass(frozen=True)
class Principal:
    """已认证主体。由服务端解析得到，不可由调用方构造传入工具。"""

    tenant_id: str
    user_id: str
    role: str


@dataclass(frozen=True)
class ToolContext:
    """工具执行上下文。与 Principal 一起在闭包里绑定，模型不可见、不可覆盖。"""

    principal: Principal
    run_id: str

    @property
    def tenant_id(self) -> str:
        return self.principal.tenant_id

    @property
    def role(self) -> str:
        return self.principal.role


class IdentityResolutionError(RuntimeError):
    """身份解析失败。对模型只暴露通用错误，不透露具体原因。"""


def resolve_principal(session: Session, user_id: str) -> Principal:
    """从服务端身份解析出 Principal。

    **D1 占位实现**：直接按 user_id 查库。
    真实实现由 D2 的认证中间件提供（从会话/token 解析），
    在此之前本函数不得用于任何非合成环境。
    """
    if not user_id or not user_id.strip():
        raise IdentityResolutionError("user_id 为空，无法解析身份")

    user = UserRepository(session).get_user_by_id(user_id)
    if user is None:
        raise IdentityResolutionError("身份解析失败")

    return Principal(tenant_id=user.tenant_id, user_id=user.user_id, role=user.role)
