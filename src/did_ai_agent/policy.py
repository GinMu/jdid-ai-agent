"""策略层:白名单校验、参数校验、脱敏、限流。

这一层是"AI 不能为所欲为"的落点,所有工具调用在进图之前都要过它。
"""

from __future__ import annotations

import time
from collections import defaultdict, deque
from typing import Any

from .tools.registry import TOOLS

# 任何情况下都不该出现在日志/审计/事件里的键
SENSITIVE_KEYS = frozenset(
    {
        "privatekey",
        "private_key",
        "mnemonic",
        "seed",
        "seedphrase",
        "secret",
        "password",
        "passphrase",
        "keystore",
    }
)


class PolicyError(Exception):
    """策略拒绝:未知工具、参数不合法、超限。"""


def _is_type(value: Any, declared: str) -> bool:
    match declared:
        case "string":
            return isinstance(value, str)
        case "integer":
            return isinstance(value, int) and not isinstance(value, bool)
        case "number":
            return isinstance(value, (int, float)) and not isinstance(value, bool)
        case "boolean":
            return isinstance(value, bool)
        case "object":
            return isinstance(value, dict)
        case "array":
            return isinstance(value, list)
        case _:
            return True


def validate_call(tool_name: str, args: dict[str, Any] | None) -> dict[str, Any]:
    """校验工具名在**白名单**内、参数符合 schema;返回规整后的参数。"""
    tool = TOOLS.get(tool_name)
    if tool is None:
        raise PolicyError(f"未知工具:{tool_name}")
    if args is None:
        args = {}
    if not isinstance(args, dict):
        raise PolicyError("工具参数必须是对象")

    schema = tool.params
    properties: dict[str, Any] = schema.get("properties", {})
    required: list[str] = schema.get("required", [])

    for key in required:
        if key not in args:
            raise PolicyError(f"{tool_name} 缺少必填参数:{key}")
    if schema.get("additionalProperties") is False:
        extra = set(args) - set(properties)
        if extra:
            raise PolicyError(f"{tool_name} 不接受参数:{sorted(extra)}")
    for key, value in args.items():
        declared = (properties.get(key) or {}).get("type")
        if declared and not _is_type(value, declared):
            raise PolicyError(f"{tool_name}.{key} 类型应为 {declared}")
    return args


def redact(value: Any) -> Any:
    """递归去掉敏感键(用于审计与日志;模型看到的数据由钱包侧裁剪,这里再兜一层)。"""
    if isinstance(value, dict):
        return {
            k: ("***" if k.replace("-", "_").lower() in SENSITIVE_KEYS else redact(v))
            for k, v in value.items()
        }
    if isinstance(value, list):
        return [redact(v) for v in value]
    return value


class RateLimiter:
    """每个会话每分钟最多 N 次(滑动窗口)。"""

    def __init__(self, per_minute: int) -> None:
        self.per_minute = per_minute
        self._hits: dict[str, deque[float]] = defaultdict(deque)

    def check(self, session_id: str) -> None:
        now = time.monotonic()
        window = self._hits[session_id]
        while window and now - window[0] > 60:
            window.popleft()
        if len(window) >= self.per_minute:
            raise PolicyError("请求过于频繁,请稍后再试")
        window.append(now)
