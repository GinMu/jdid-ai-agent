"""HTTP 契约:请求体与 SSE 事件。

事件只有四种,钱包只认这四种:
- text        流式/整段文本
- tool_request 需要钱包本机执行的工具(写操作会带 kind="confirm")
- error       失败(可展示给人)
- done        本轮结束
"""

from typing import Any, Literal

from pydantic import BaseModel, Field


class ChatRequest(BaseModel):
    session_id: str = Field(min_length=1, max_length=128)
    message: str = Field(min_length=1, max_length=4000)


class ResumeRequest(BaseModel):
    session_id: str = Field(min_length=1, max_length=128)
    approved: bool | None = None
    result: Any | None = None
    reason: str | None = None


class Event(BaseModel):
    type: Literal["text", "tool_request", "error", "done"]
    text: str | None = None
    tool: str | None = None
    kind: Literal["read", "confirm"] | None = None
    args: dict[str, Any] | None = None
    request_id: str | None = None
    error: str | None = None
