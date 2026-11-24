"""FastAPI 外壳:/healthz、/chat(SSE)、/resume(SSE)、/audit。

钱包只需要认 SSE 里四种事件:text / tool_request / error / done。
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from typing import Any

from fastapi import FastAPI
from fastapi.responses import StreamingResponse
from langgraph.types import Command

from . import __version__
from .audit import AuditLog
from .config import get_settings
from .graph import build_graph
from .planner import build_planner
from .policy import PolicyError, RateLimiter
from .protocol import ChatRequest, Event, ResumeRequest
from .tools.registry import TOOLS

settings = get_settings()
planner = build_planner(settings)
graph = build_graph(planner, max_turns=settings.max_turns)
audit = AuditLog(settings.audit_size)
limiter = RateLimiter(settings.rate_limit_per_min)

app = FastAPI(title="JDID AI Agent", version=__version__)


def _sse(event: Event) -> str:
    return f"data: {event.model_dump_json(exclude_none=True)}\n\n"


def _config(session_id: str) -> dict[str, Any]:
    return {"configurable": {"thread_id": session_id}}


def _translate(chunk: dict[str, Any], session_id: str) -> Iterator[Event]:
    """把 LangGraph 的 updates 翻成钱包认识的四种事件。"""
    for node, update in chunk.items():
        if node == "__interrupt__":
            payload = update[0].value if isinstance(update, (list, tuple)) and update else update
            audit.record(
                session_id,
                "tool_request",
                tool=payload.get("tool"),
                detail={"kind": payload.get("kind")},
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
        if node == "plan" and update.get("final"):
            audit.record(session_id, "answer")
            yield Event(type="text", text=update["final"])
        if node == "request_tool":
            audit.record(session_id, "tool_result", detail={"turns": update.get("turns")})


def _run(input_or_command: Any, session_id: str) -> Iterator[Event]:
    try:
        for chunk in graph.stream(input_or_command, _config(session_id), stream_mode="updates"):
            yield from _translate(chunk, session_id)
    except PolicyError as exc:
        yield Event(type="error", error=str(exc))
    yield Event(type="done")


@app.get("/healthz")
def healthz() -> dict[str, Any]:
    return {
        "ok": True,
        "version": __version__,
        "planner": type(planner).__name__,
        "tools": len(TOOLS),
        "model": settings.model or None,
    }


@app.get("/audit")
def audit_entries() -> list[dict[str, Any]]:
    return [
        {"at": e.at, "session": e.session_id, "event": e.event, "tool": e.tool, "detail": e.detail}
        for e in audit.entries()
    ]


@app.post("/chat")
def chat(req: ChatRequest) -> StreamingResponse:
    limiter.check(req.session_id)
    audit.record(req.session_id, "chat", detail={"chars": len(req.message)})
    body = {"session_id": req.session_id, "message": req.message, "turns": 0, "pending": None}
    return StreamingResponse(
        (_sse(e) for e in _run(body, req.session_id)), media_type="text/event-stream"
    )


@app.post("/resume")
def resume(req: ResumeRequest) -> StreamingResponse:
    decision: dict[str, Any]
    if req.approved is not None:
        decision = {"approved": req.approved}
        if req.reason:
            decision["reason"] = req.reason
    else:
        decision = {"result": req.result}
    audit.record(req.session_id, "resume", detail={"approved": req.approved})
    return StreamingResponse(
        (_sse(e) for e in _run(Command(resume=decision), req.session_id)),
        media_type="text/event-stream",
    )


def _parse_body(raw: bytes) -> Any:  # 仅供调试脚本使用
    return json.loads(raw or b"{}")
