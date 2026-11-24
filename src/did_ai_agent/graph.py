"""LangGraph 编排:plan → (工具往返 | 结束)。

关键:工具**不在服务端执行**。需要工具时调用 `interrupt`,把请求交回钱包;
钱包在本机执行(写操作还要用户确认)后用 resume 把结果送回来,图才继续。

用 LangGraph 的理由就是这个:`interrupt/resume` 让"等人确认"成为流程的一等公民,
并且 checkpointer 让同一个会话可以断点续跑。
"""

from __future__ import annotations

from typing import Any, TypedDict

from langgraph.checkpoint.memory import MemorySaver
from langgraph.graph import END, START, StateGraph
from langgraph.types import interrupt

from .planner import Planner
from .policy import PolicyError, redact, validate_call
from .tools.registry import TOOLS


class AgentState(TypedDict, total=False):
    session_id: str
    message: str
    history: list[dict[str, Any]]
    pending: dict[str, Any] | None
    final: str | None
    turns: int
    error: str | None


def _explain(exc: Exception) -> str:
    """把 provider 的常见 4xx 翻译成可执行的提示;其它错误原样带出。"""
    text = str(exc)
    if "supported API model names" in text or "model_not_found" in text:
        return f"{text}(提示:换成服务端支持的名字,如 deepseek:deepseek-flash)"
    if "401" in text or "auth" in text.lower():
        return f"{text}(提示:检查 DEEPSEEK_API_KEY)"
    return text


def build_graph(planner: Planner, max_turns: int = 6) -> Any:
    def plan_node(state: AgentState) -> dict[str, Any]:
        if state.get("turns", 0) >= max_turns:
            return {
                "final": "这轮已经查得够多了,先到这里;需要更深的排查可以让技术人员介入。",
                "pending": None,
            }
        try:
            plan = planner.plan(state.get("message", ""), list(state.get("history", [])))
        except Exception as exc:  # 规划失败也要变成可展示的信息
            return {
                "error": (
                    f"规划失败:{exc}。"
                    "常见原因:DID_AI_MODEL 不是服务端支持的模型名"
                    "(deepseek-flash / deepseek-v4-pro),"
                    "或 DEEPSEEK_API_KEY 无效。"
                ),
                "pending": None,
            }
        if plan.tool is None:
            return {"final": plan.text or "我没有更多信息了。", "pending": None}
        try:
            args = validate_call(plan.tool, plan.args)
        except PolicyError as exc:
            return {"error": str(exc), "pending": None}
        kind = "confirm" if TOOLS[plan.tool].kind == "write" else "read"
        return {
            "pending": {
                "tool": plan.tool,
                "kind": kind,
                "args": redact(args),
                "tool_call_id": plan.tool_call_id,
            },
            "final": None,
        }

    def request_tool_node(state: AgentState) -> dict[str, Any]:
        pending = state["pending"]
        decision = interrupt(
            {
                "tool": pending["tool"],
                "kind": pending["kind"],
                "args": pending["args"],
                "request_id": (
                    f"{state.get('session_id', '')}:{pending['tool']}:{state.get('turns', 0)}"
                ),
            }
        )
        history = list(state.get("history", []))
        history.append(
            {
                "tool": pending["tool"],
                "args": pending["args"],
                "tool_call_id": pending.get("tool_call_id"),
                "result": decision,
            }
        )
        return {"history": history, "pending": None, "turns": state.get("turns", 0) + 1}

    def finish_node(state: AgentState) -> dict[str, Any]:
        return {"final": state.get("final") or "完成。"}

    def route(state: AgentState) -> str:
        if state.get("error"):
            return "finish"
        if state.get("pending"):
            return "request_tool"
        return "finish"

    builder = StateGraph(AgentState)
    builder.add_node("plan", plan_node)
    builder.add_node("request_tool", request_tool_node)
    builder.add_node("finish", finish_node)
    builder.add_edge(START, "plan")
    builder.add_conditional_edges(
        "plan", route, {"request_tool": "request_tool", "finish": "finish"}
    )
    builder.add_edge("request_tool", "plan")
    builder.add_edge("finish", END)
    return builder.compile(checkpointer=MemorySaver())
