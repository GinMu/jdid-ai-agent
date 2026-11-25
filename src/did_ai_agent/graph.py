"""LangGraph 编排:plan → (工具往返 | 结束)。

用一句话说清这个流程:

    START → plan ──有工具要调──→ request_tool ──resume 拿到结果──→ plan …(循环)
               └──直接回答──→ finish → END

两条设计原则(看懂这两个,整个文件就看懂了):

1. **工具不在服务端执行**。需要数据时,`request_tool` 调 LangGraph 的 `interrupt()`
   把请求"挂起"并交回钱包;钱包在本机执行(写操作还要用户点确认),再调 `/resume`
   把结果送回来,图从断点继续。这样私钥永远不出设备。
2. **一句话可能要走好几步**(多步规划):拿到工具结果后回到 `plan` 再判断一次,
   直到模型给出最终回答,或者达到 `max_turns` 上限(防止无限循环)。

`MemorySaver` 保存每个会话的状态,`thread_id` 就是会话 id —— 断线重连也能接着跑。
"""

from __future__ import annotations

from typing import Any, TypedDict

from langgraph.checkpoint.memory import MemorySaver
from langgraph.config import get_stream_writer
from langgraph.graph import END, START, StateGraph
from langgraph.types import interrupt

from .logging_setup import log_tool_result
from .planner import Planner
from .policy import PolicyError, redact, validate_call
from .tools.registry import TOOLS


class AgentState(TypedDict, total=False):
    """会话状态。LangGraph 每个节点读它、返回"要更新的字段"。"""

    session_id: str
    message: str  # 用户这一轮说的话
    history: list[dict[str, Any]]  # 已经执行过的工具调用(工具名/参数/结果/id)
    pending: dict[str, Any] | None  # 正准备交给钱包执行的工具调用
    final: str | None  # 最终回答
    turns: int  # 已经走了几次工具往返(防死循环)
    error: str | None


def _explain(exc: Exception) -> str:
    """把 provider 的常见 4xx 翻译成可执行的提示;其它错误原样带出。"""
    text = str(exc)
    if "supported API model names" in text or "model_not_found" in text:
        return f"{text}(提示:换成服务端支持的名字,如 deepseek:deepseek-flash)"
    if "401" in text or "auth" in text.lower():
        return f"{text}(提示:检查 DEEPSEEK_API_KEY)"
    return text


def _text_writer() -> Any:
    """拿到"自定义流"的写入器;不在流式上下文里时退化成空函数。

    LangGraph 的自定义流:节点里 writer({...}) 的内容会原样出现在
    `graph.stream(..., stream_mode="custom")` 里,我们用它来逐 token 推文本。
    """
    try:
        return get_stream_writer()
    except Exception:  # pragma: no cover - 仅在没有自定义流时触发
        return lambda _payload: None


def _stream_text(planner: Planner, message: str, history: list[dict[str, Any]]) -> Any:
    """跑一遍规划器,把文本片段实时推给前端,返回最终 Plan(或 None)。

    这里同时兼容两种规划器:
    - 真实模型:先吐出很多 `Delta(text=...)`,最后一个 `Delta(plan=...)`;
    - FakePlanner:直接一个 `Delta(plan=...)`,没有文本片段。
    """
    writer = _text_writer()
    plan = None
    for delta in planner.iter_plan(message, history):
        if delta.text:
            writer({"type": "text", "text": delta.text})
        if delta.plan is not None:
            plan = delta.plan
    return plan


def build_graph(planner: Planner, max_turns: int = 6) -> Any:
    """编译图。`planner` 可注入(FakePlanner 用于离线测试)。"""

    def plan_node(state: AgentState) -> dict[str, Any]:
        """决定下一步:调工具,还是直接回答。"""
        # 保险丝:工具往返太多就停下来,避免模型绕圈烧钱
        if state.get("turns", 0) >= max_turns:
            return {
                "final": "这轮已经查得够多了,先到这里;需要更深的排查可以让技术人员介入。",
                "pending": None,
            }

        try:
            plan = _stream_text(planner, state.get("message", ""), list(state.get("history", [])))
        except Exception as exc:  # 规划失败也要变成用户能看懂的信息
            return {"error": f"规划失败:{_explain(exc)}", "pending": None}
        if plan is None:
            return {"error": "规划失败:模型没有返回结果", "pending": None}

        # 不需要工具 → 直接把文字作为最终回答
        if plan.tool is None:
            return {"final": plan.text or "我没有更多信息了。", "pending": None}

        # 需要工具 → 先过策略校验(白名单 + 参数 schema),不合格就拒绝
        try:
            args = validate_call(plan.tool, plan.args)
        except PolicyError as exc:
            return {"error": str(exc), "pending": None}

        kind = "confirm" if TOOLS[plan.tool].kind == "write" else "read"
        return {
            "pending": {
                "tool": plan.tool,
                "kind": kind,
                # 审计/事件里只放脱敏后的参数(私钥类字段变 ***)
                "args": redact(args),
                "tool_call_id": plan.tool_call_id,
            },
            "final": None,
        }

    def request_tool_node(state: AgentState) -> dict[str, Any]:
        """把工具调用交给钱包,并在这里"挂起"等 resume。

        注意:`interrupt()` 第二次被调用时(同一节点重放)会直接返回 resume 的值,
        所以这个函数看起来"没写等待逻辑",实际等待由 LangGraph 处理。
        """
        pending = state["pending"]
        # 注意:不要在这里打"工具请求"日志 —— 恢复时 LangGraph 会从头重跑本节点,
        # interrupt() 之前的副作用会执行两次。请求日志统一放在 app 层(那里只发一次事件)。
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
        # 把这次调用的结果记进历史 —— 下一轮 plan 就能看到它,从而实现多步规划
        # 日志:钱包回来了什么(摘要;过长会截断)
        log_tool_result(state.get("session_id", ""), pending["tool"], decision)
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
        """收尾:把最终文本固定在 state 里(方便 /audit 与调试)。"""
        return {"final": state.get("final") or "完成。"}

    def route(state: AgentState) -> str:
        """条件边:有错误 → 结束;有 pending → 请求工具;否则 → 结束。"""
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
    builder.add_edge("request_tool", "plan")  # ← 多步规划的关键:回到 plan 再判断一次
    builder.add_edge("finish", END)
    return builder.compile(checkpointer=MemorySaver())
