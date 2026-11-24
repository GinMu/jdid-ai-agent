"""多步规划 + 流式接线(graph 层,离线)。

两个场景:
1. 一句话需要连着查两次(先列凭证、再逐张验证),最后才总结 —— 证明 request_tool → plan 的循环;
2. 节点里写出的文本片段能通过 LangGraph 的 `custom` 流出来 —— 证明流式接线。
"""

from langgraph.types import Command

from did_ai_agent.graph import build_graph
from did_ai_agent.planner import Delta, Plan, Planner


class ScriptedPlanner(Planner):
    """按脚本出牌:第 N 次调用返回脚本里的第 N 项。"""

    def __init__(self, script: list[Plan]) -> None:
        self._script = script
        self.calls = 0

    def plan(self, message, history):
        plan = self._script[min(self.calls, len(self._script) - 1)]
        self.calls += 1
        return plan


class StreamingPlanner(Planner):
    """模拟真实模型:先吐三段文本,最后给一个纯文本计划。"""

    def plan(self, message, history):
        return Plan(text="流式回答")

    def iter_plan(self, message, history):
        yield Delta(text="流式")
        yield Delta(text="回答")
        yield Delta(plan=Plan(text="流式回答"))


def _interrupt(chunks):
    for chunk in chunks:
        if "__interrupt__" in chunk:
            return chunk["__interrupt__"][0].value
    return None


def test_one_question_can_chain_two_tools_then_answer():
    planner = ScriptedPlanner(
        [
            Plan(tool="list_credentials", args={"limit": 3}, tool_call_id="c1"),
            Plan(tool="verify_credential", args={"credential_id": "vc-1"}, tool_call_id="c2"),
            Plan(text="两张都有效。"),
        ]
    )
    graph = build_graph(planner)
    cfg = {"configurable": {"thread_id": "multi-1"}}

    first = list(
        graph.stream(
            {
                "session_id": "multi-1",
                "message": "帮我确认凭证还有效吗",
                "turns": 0,
                "pending": None,
            },
            cfg,
            stream_mode="updates",
        )
    )
    assert _interrupt(first)["tool"] == "list_credentials"

    second = list(
        graph.stream(
            Command(resume={"result": [{"id": "vc-1"}, {"id": "vc-2"}]}),
            cfg,
            stream_mode="updates",
        )
    )
    assert _interrupt(second)["tool"] == "verify_credential"  # ← 多步:第二次工具调用

    third = list(
        graph.stream(Command(resume={"result": {"verified": True}}), cfg, stream_mode="updates")
    )
    assert _interrupt(third) is None
    finals = [c["plan"]["final"] for c in third if "plan" in c and c["plan"].get("final")]
    assert finals and "有效" in finals[0]


def test_turn_cap_bounds_the_chain():
    planner = ScriptedPlanner([Plan(tool="get_identity", args={}, tool_call_id="c1")])
    graph = build_graph(planner, max_turns=1)
    cfg = {"configurable": {"thread_id": "multi-2"}}

    first = list(
        graph.stream(
            {"session_id": "multi-2", "message": "查身份", "turns": 0, "pending": None},
            cfg,
            stream_mode="updates",
        )
    )
    assert _interrupt(first)["tool"] == "get_identity"

    second = list(graph.stream(Command(resume={"result": {}}), cfg, stream_mode="updates"))
    assert _interrupt(second) is None  # 达到上限,不再请求工具
    assert any("查得够多" in str(c) for c in second)


def test_text_deltas_reach_the_custom_stream():
    graph = build_graph(StreamingPlanner())
    cfg = {"configurable": {"thread_id": "stream-1"}}
    pieces: list[str] = []
    for mode, chunk in graph.stream(
        {"session_id": "stream-1", "message": "你好", "turns": 0, "pending": None},
        cfg,
        stream_mode=["updates", "custom"],
    ):
        if mode == "custom":
            pieces.append(chunk["text"])

    assert pieces == ["流式", "回答"], pieces
