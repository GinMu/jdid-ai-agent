from langgraph.types import Command

from did_ai_agent.graph import build_graph
from did_ai_agent.planner import FakePlanner


def _first_turn(graph, session: str, message: str):
    cfg = {"configurable": {"thread_id": session}}
    body = {"session_id": session, "message": message, "turns": 0, "pending": None}
    return list(graph.stream(body, cfg, stream_mode="updates"))


def _resume(graph, session: str, decision):
    cfg = {"configurable": {"thread_id": session}}
    return list(graph.stream(Command(resume=decision), cfg, stream_mode="updates"))


def _interrupt_payload(chunks):
    for chunk in chunks:
        if "__interrupt__" in chunk:
            return chunk["__interrupt__"][0].value
    return None


def test_read_tool_round_trip_then_summarize():
    graph = build_graph(FakePlanner())
    payload = _interrupt_payload(_first_turn(graph, "g1", "看看我的凭证"))
    assert payload == {
        "tool": "list_credentials",
        "kind": "read",
        "args": {"limit": 20},
        "request_id": "g1:list_credentials:0",
    }

    chunks = _resume(graph, "g1", {"result": [{"type": "A"}, {"type": "B"}]})
    text = [c["plan"]["final"] for c in chunks if "plan" in c and c["plan"].get("final")]
    assert text, chunks
    assert "2" in text[0]


def test_write_tool_is_marked_confirm():
    graph = build_graph(FakePlanner())
    payload = _interrupt_payload(_first_turn(graph, "g2", "帮我签一条登录消息"))
    assert payload["tool"] == "propose_sign_message"
    assert payload["kind"] == "confirm"


def test_unknown_capability_still_answers_without_tools():
    graph = build_graph(FakePlanner())
    chunks = _first_turn(graph, "g3", "你好")
    assert _interrupt_payload(chunks) is None
    assert any(c.get("plan", {}).get("final") for c in chunks)


def test_turn_cap_stops_the_loop():
    graph = build_graph(FakePlanner(), max_turns=0)
    chunks = _first_turn(graph, "g4", "看看我的凭证")
    assert _interrupt_payload(chunks) is None
    assert any("查得够多" in str(c) for c in chunks)


def test_interrupt_is_redacted():
    graph = build_graph(FakePlanner())
    payload = _interrupt_payload(_first_turn(graph, "g5", "看看我的凭证"))
    assert "privateKey" not in str(payload)


def test_knowledge_tool_runs_on_the_server_without_asking_the_wallet():
    """query_knowledge 是服务端本地工具:不产生 tool_request,也不等 resume。"""
    graph = build_graph(FakePlanner())
    cfg = {"configurable": {"thread_id": "local-tool-1"}}
    body = {"session_id": "local-tool-1", "message": "介绍一下 did:swtc 方法", "turns": 0}
    events = list(graph.stream(body, cfg, stream_mode="updates"))
    # 一轮就把知识库检索跑完并给出回答:没有 __interrupt__
    assert not any("__interrupt__" in event for event in events), events
    state = graph.get_state(cfg).values
    assert state["history"][0]["tool"] == "query_knowledge"
    assert state["history"][0]["result"]["total"] >= 1
    # 模型侧只该看到正文:不要 id/来源/分数,避免回答里罗列"命中"
    assert "hits" not in state["history"][0]["result"]
    assert state["history"][0]["result"]["passages"]
    assert state.get("final")
