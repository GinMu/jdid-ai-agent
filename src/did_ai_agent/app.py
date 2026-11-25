"""FastAPI 外壳:/healthz、/chat(SSE)、/resume(SSE)、/audit。

钱包只需要认 SSE 里四种事件:

    text          文本片段(流式时会有很多条)
    tool_request  需要钱包**本机**执行的工具;写操作 kind="confirm"
    error         失败(可直接展示给人)
    done          本轮结束

为什么用 SSE 而不是 WebSocket:单向流、走普通 HTTP、断线重连简单,钱包两端都容易实现。
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

from fastapi import FastAPI
from fastapi.responses import StreamingResponse
from langgraph.types import Command

from . import __version__
from .audit import AuditLog
from .config import get_settings
from .graph import build_graph
from .logging_setup import configure as configure_logging
from .logging_setup import log_chat, log_resume, log_tool_request
from .planner import build_planner
from .policy import PolicyError, RateLimiter
from .protocol import ChatRequest, Event, ResumeRequest
from .tools.registry import TOOLS

# ── 进程级单例(演示够用;生产应换成外部存储 + 多实例共享)────────────────
settings = get_settings()
planner = build_planner(settings)  # 默认 FakePlanner;配了模型才是 LangChainPlanner
graph = build_graph(planner, max_turns=settings.max_turns)
audit = AuditLog(settings.audit_size)  # 内存环形缓冲,只记事件/工具,不记原文
limiter = RateLimiter(settings.rate_limit_per_min)

LOG_FILE = configure_logging()

app = FastAPI(title="JDID AI Agent", version=__version__)


def _sse(event: Event) -> str:
    """把事件序列化成一行 SSE:`data: {json}` + 空行。"""
    return f"data: {event.model_dump_json(exclude_none=True)}\n\n"


def _config(session_id: str) -> dict[str, Any]:
    """LangGraph 的会话配置:`thread_id` 就是我们的 session_id(断点续跑靠它)。"""
    return {"configurable": {"thread_id": session_id}}


def _translate(chunk: dict[str, Any], session_id: str) -> Iterator[Event]:
    """把一个 LangGraph 的 `updates` 分片翻译成钱包认识的事件。

    `updates` 的形状是 `{节点名: 该节点返回的状态更新}`;`__interrupt__` 是特殊键,
    表示"图在这里挂起了,等钱包执行工具"。
    """
    for node, update in chunk.items():
        if node == "__interrupt__":
            # LangGraph 把挂起信息放在一个 Interrupt 对象里,`.value` 就是我们传的 payload
            payload = update[0].value if isinstance(update, (list, tuple)) and update else update
            audit.record(
                session_id,
                "tool_request",
                tool=payload.get("tool"),
                detail={"kind": payload.get("kind")},
            )
            # 每次挂起只打一次(这个分支对应 SSE 的 tool_request 事件)
            log_tool_request(
                session_id,
                str(payload.get("tool")),
                payload.get("args"),
                str(payload.get("kind")),
            )
            yield Event(
                type="tool_request",
                tool=payload.get("tool"),
                kind=payload.get("kind"),
                args=payload.get("args"),
                request_id=payload.get("request_id"),
            )
            continue
        if not isinstance(update, dict):
            continue
        if update.get("error"):
            audit.record(session_id, "error", detail={"error": update["error"]})
            yield Event(type="error", error=str(update["error"]))
        if node == "request_tool":
            # 工具结果已经写进历史(说明钱包已经回来了)
            audit.record(session_id, "tool_result", detail={"turns": update.get("turns")})


def _run(input_or_command: Any, session_id: str) -> Iterator[Event]:
    """跑一次图,并把它产生的所有东西翻译成 SSE 事件。

    同时订阅两种流:
    - `updates`:节点返回的状态更新(用来发现 tool_request / error);
    - `custom` :节点里 writer() 推出来的内容(用来逐 token 发 text)。
    """
    streamed = False  # 本轮是否已经流过文本(避免和最终回答重复)
    try:
        for mode, chunk in graph.stream(
            input_or_command, _config(session_id), stream_mode=["updates", "custom"]
        ):
            if mode == "custom":
                text = (chunk or {}).get("text")
                if text:
                    streamed = True
                    yield Event(type="text", text=text)
                continue

            for event in _translate(chunk, session_id):
                if event.type == "tool_request":
                    # 这一轮到此为止(等钱包执行),下一轮的流式状态重置
                    streamed = False
                yield event

            # 没有流式文本(比如 FakePlanner)时,把最终回答整段发出去
            for node, update in chunk.items():
                if (
                    node == "plan"
                    and isinstance(update, dict)
                    and update.get("final")
                    and not streamed
                ):
                    audit.record(session_id, "answer")
                    yield Event(type="text", text=update["final"])
    except PolicyError as exc:
        yield Event(type="error", error=str(exc))
    yield Event(type="done")


@app.get("/healthz")
def healthz() -> dict[str, Any]:
    """健康检查:顺便告诉调用方现在用的是哪种规划器。"""
    return {
        "ok": True,
        "version": __version__,
        "planner": type(planner).__name__,
        "tools": len(TOOLS),
        "model": settings.model or None,
        "logFile": str(LOG_FILE),
    }


@app.get("/audit")
def audit_entries() -> list[dict[str, Any]]:
    """调试用:看最近发生过什么(只含事件/工具/数据类别)。"""
    return [
        {"at": e.at, "session": e.session_id, "event": e.event, "tool": e.tool, "detail": e.detail}
        for e in audit.entries()
    ]


@app.post("/chat")
def chat(req: ChatRequest) -> StreamingResponse:
    """用户说一句话。可能直接回答,也可能要求钱包执行一个工具。"""
    limiter.check(req.session_id)
    audit.record(req.session_id, "chat", detail={"chars": len(req.message)})
    log_chat(req.session_id, len(req.message))
    body = {"session_id": req.session_id, "message": req.message, "turns": 0, "pending": None}
    return StreamingResponse(
        (_sse(e) for e in _run(body, req.session_id)), media_type="text/event-stream"
    )


@app.post("/resume")
def resume(req: ResumeRequest) -> StreamingResponse:
    """钱包把工具结果/用户决定送回来,图从断点继续。"""
    decision: dict[str, Any]
    if req.approved is not None:
        # 写操作:用户点了确认/拒绝
        decision = {"approved": req.approved}
        if req.reason:
            decision["reason"] = req.reason
    else:
        # 读操作:直接回传数据
        decision = {"result": req.result}
    audit.record(req.session_id, "resume", detail={"approved": req.approved})
    # 把钱包回传的内容完整记下来(排查"模型看到了什么"最直接的一条)
    log_resume(req.session_id, decision)
    return StreamingResponse(
        (_sse(e) for e in _run(Command(resume=decision), req.session_id)),
        media_type="text/event-stream",
    )
