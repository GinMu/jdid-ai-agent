"""把注册表转成模型可用的工具 schema(OpenAI function-calling 形状)。"""

from typing import Any

from .registry import READ_TOOLS, WRITE_TOOLS, Tool


def _one(tool: Tool) -> dict[str, Any]:
    return {
        "type": "function",
        "function": {
            "name": tool.name,
            "description": tool.description,
            "parameters": tool.params,
        },
    }


def tool_schemas() -> list[dict[str, Any]]:
    """读 + 写都给模型看:写工具只是"提案",真正执行要用户确认。"""
    return [_one(t) for t in (*READ_TOOLS, *WRITE_TOOLS)]
