"""用本地 OpenAI 兼容桩验证真实模型链路:流式、工具选择、多步、白名单。

不打外网、不需要 key —— 桩服务模拟 `stream=true` 的 SSE 响应。
"""

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from did_ai_agent.planner import LangChainPlanner

MODEL = "deepseek-flash"


def _chunk(delta: dict, finish_reason: str | None = None) -> dict:
    return {
        "id": "chatcmpl-test",
        "object": "chat.completion.chunk",
        "created": 0,
        "model": MODEL,
        "choices": [{"index": 0, "delta": delta, "finish_reason": finish_reason}],
    }


def _text_stream(*parts: str) -> list[dict]:
    """模拟一段纯文本回答(分片返回)。"""
    body = [_chunk({"role": "assistant", "content": ""})]
    body += [_chunk({"content": part}) for part in parts]
    body.append(_chunk({}, "stop"))
    return body


def _tool_stream(name: str, args: dict, call_id: str = "call_1") -> list[dict]:
    """模拟模型要求调一个工具(OpenAI 的流式 tool_calls 是分片拼起来的)。"""
    return [
        _chunk({"role": "assistant", "content": ""}),
        _chunk(
            {
                "tool_calls": [
                    {
                        "index": 0,
                        "id": call_id,
                        "type": "function",
                        "function": {"name": name, "arguments": ""},
                    }
                ]
            }
        ),
        _chunk(
            {
                "tool_calls": [
                    {"index": 0, "function": {"arguments": json.dumps(args, ensure_ascii=False)}}
                ]
            }
        ),
        _chunk({}, "tool_calls"),
    ]


@pytest.fixture
def stub():
    """桩服务:按队列依次返回预设的响应,并记录每次请求体。"""
    state: dict = {"queue": [], "requests": []}

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.0"  # 写完就关连接,客户端读到 EOF 即结束

        def do_POST(self):  # noqa: N802
            length = int(self.headers.get("content-length", 0))
            body = json.loads(self.rfile.read(length) or b"{}")
            state["requests"].append(body)

            chunks = state["queue"].pop(0) if state["queue"] else _text_stream("ok")
            self.send_response(200)
            self.send_header("content-type", "text/event-stream")
            self.send_header("cache-control", "no-cache")
            self.send_header("connection", "close")
            self.end_headers()
            for chunk in chunks:
                self.wfile.write(f"data: {json.dumps(chunk)}\n\n".encode())
                self.wfile.flush()
            self.wfile.write(b"data: [DONE]\n\n")
            self.wfile.flush()
            self.close_connection = True

        def log_message(self, *args):
            return

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}/v1", state
    finally:
        server.shutdown()
        thread.join(timeout=5)


def _planner(base_url: str) -> LangChainPlanner:
    return LangChainPlanner.from_config(
        f"deepseek:{MODEL}", temperature=0, api_key="test-key", base_url=base_url
    )


def test_tool_call_is_parsed_into_a_plan(stub):
    base_url, state = stub
    state["queue"] = [_tool_stream("list_credentials", {"limit": 5})]
    plan = _planner(base_url).plan("看看我的凭证", [])
    assert plan.tool == "list_credentials"
    assert plan.args == {"limit": 5}
    assert plan.tool_call_id == "call_1"


def test_text_answer_is_streamed_in_pieces_then_assembled(stub):
    base_url, state = stub
    state["queue"] = [_text_stream("你好,", "我可以", "帮你查凭证。")]

    deltas = list(_planner(base_url).iter_plan("你好", []))
    text_pieces = [d.text for d in deltas if d.text]
    assert "".join(text_pieces) == "你好,我可以帮你查凭证。"
    assert len(text_pieces) >= 2, "文本应该被分成多个片段(流式)"
    final = [d.plan for d in deltas if d.plan][-1]
    assert final.tool is None
    assert "查凭证" in (final.text or "")


def test_unknown_tool_name_from_model_is_ignored(stub):
    base_url, state = stub
    state["queue"] = [_tool_stream("rm_rf_everything", {})]
    plan = _planner(base_url).plan("随便", [])
    assert plan.tool is None  # 白名单外的名字不会被采纳


def test_tool_result_is_replayed_as_a_user_message(stub):
    """工具结果以用户消息回放:避开 assistant(tool_calls) 配对与 reasoning_content 两条协议约束。"""
    base_url, state = stub
    state["queue"] = [_text_stream("好的")]
    history = [
        {
            "tool": "list_credentials",
            "args": {"limit": 5},
            "tool_call_id": "call_1",
            "result": {"total": 7},
        }
    ]
    _planner(base_url).plan("逐个验证", history)

    messages = state["requests"][-1]["messages"]
    roles = [m["role"] for m in messages]
    assert "tool" not in roles, "不再发送 tool 角色消息"
    last = messages[-1]
    assert last["role"] == "user"
    assert "list_credentials" in last["content"] and "7" in last["content"]
