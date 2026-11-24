"""用本地 OpenAI 兼容桩验证 LangChainPlanner 的真实链路(不打外网、不需要 key)。

桩服务只回一个 /v1/chat/completions,分别模拟两种响应:
- tool_calls:模型要求调 list_credentials
- 纯文本:模型直接回答
"""

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from did_ai_agent.planner import LangChainPlanner

TOOL_CALL_BODY = {
    "id": "chatcmpl-test",
    "object": "chat.completion",
    "created": 0,
    "model": "deepseek-flash",
    "choices": [
        {
            "index": 0,
            "finish_reason": "tool_calls",
            "message": {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {
                        "id": "call_1",
                        "type": "function",
                        "function": {
                            "name": "list_credentials",
                            "arguments": json.dumps({"limit": 5}),
                        },
                    }
                ],
            },
        }
    ],
    "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
}

TEXT_BODY = {
    "id": "chatcmpl-test",
    "object": "chat.completion",
    "created": 0,
    "model": "deepseek-flash",
    "choices": [
        {
            "index": 0,
            "finish_reason": "stop",
            "message": {"role": "assistant", "content": "你好,我可以帮你查凭证。"},
        }
    ],
    "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
}


@pytest.fixture
def stub_model():
    body = {"value": TOOL_CALL_BODY, "last_request": {}}

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):  # noqa: N802
            length = int(self.headers.get("content-length", 0))
            body["last_request"] = json.loads(self.rfile.read(length) or b"{}")
            payload = json.dumps(body["value"]).encode()
            self.send_response(200)
            self.send_header("content-type", "application/json")
            self.send_header("content-length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def log_message(self, *args):  # 静音
            return

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}/v1", body
    finally:
        server.shutdown()
        thread.join(timeout=5)


def _planner(base_url: str) -> LangChainPlanner:
    return LangChainPlanner.from_config(
        "deepseek:deepseek-flash", temperature=0, api_key="test-key", base_url=base_url
    )


def test_tool_call_is_parsed_into_a_plan(stub_model):
    base_url, _ = stub_model
    plan = _planner(base_url).plan("看看我的凭证", [])
    assert plan.tool == "list_credentials"
    assert plan.args == {"limit": 5}
    assert plan.text is None


def test_plain_text_answer_is_parsed_into_a_plan(stub_model):
    base_url, body = stub_model
    body["value"] = TEXT_BODY
    plan = _planner(base_url).plan("你好", [])
    assert plan.tool is None
    assert "查凭证" in (plan.text or "")


def test_unknown_tool_name_from_model_is_ignored(stub_model):
    base_url, body = stub_model
    body["value"] = json.loads(json.dumps(TOOL_CALL_BODY))
    call = body["value"]["choices"][0]["message"]["tool_calls"][0]
    call["function"]["name"] = "rm_rf_everything"
    call["function"]["arguments"] = "{}"
    plan = _planner(base_url).plan("随便", [])
    assert plan.tool is None  # 白名单外的名字不会被采纳


def test_history_is_replayed_as_assistant_tool_calls_then_tool_message(stub_model):
    """OpenAI 兼容端点要求 assistant(tool_calls) 在前、tool 结果紧跟其后,否则 400。"""
    base_url, body = stub_model
    body["value"] = TEXT_BODY
    history = [
        {
            "tool": "list_credentials",
            "args": {"limit": 5},
            "tool_call_id": "call_1",
            "result": {"result": [{"type": "A"}]},
        }
    ]
    plan = _planner(base_url).plan("这些凭证快过期吗?", history)
    assert plan.tool is None

    messages = body["last_request"]["messages"]
    roles = [m["role"] for m in messages]
    assert "assistant" in roles and "tool" in roles
    assistant_index = roles.index("assistant")
    assert messages[assistant_index]["tool_calls"][0]["id"] == "call_1"
    assert roles[assistant_index + 1] == "tool"
    assert messages[assistant_index + 1]["tool_call_id"] == "call_1"
