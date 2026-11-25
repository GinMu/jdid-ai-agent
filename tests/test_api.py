import json

from fastapi.testclient import TestClient

from did_ai_agent.app import app

client = TestClient(app)


def _events(body: str) -> list[dict]:
    out = []
    for line in body.splitlines():
        if line.startswith("data: "):
            out.append(json.loads(line[len("data: ") :]))
    return out


def test_healthz_reports_fake_planner_and_tools():
    resp = client.get("/healthz")
    assert resp.status_code == 200
    data = resp.json()
    assert data["ok"] is True
    assert data["planner"] == "FakePlanner"
    assert data["tools"] == 11


def test_chat_requests_a_read_tool_then_finishes():
    resp = client.post("/chat", json={"session_id": "api-1", "message": "看看我的凭证"})
    assert resp.status_code == 200
    events = _events(resp.text)
    kinds = [e["type"] for e in events]
    assert kinds[0] == "tool_request"
    assert kinds[-1] == "done"
    assert events[0]["tool"] == "list_credentials"
    assert events[0]["kind"] == "read"


def test_resume_returns_text_then_done():
    client.post("/chat", json={"session_id": "api-2", "message": "看看我的凭证"})
    resp = client.post("/resume", json={"session_id": "api-2", "result": [{"type": "A"}]})
    events = _events(resp.text)
    assert [e["type"] for e in events] == ["text", "done"]
    assert "1" in events[0]["text"]


def test_write_tool_arrives_as_confirm():
    resp = client.post("/chat", json={"session_id": "api-3", "message": "帮我签一条登录消息"})
    events = _events(resp.text)
    assert events[0]["tool"] == "propose_sign_message"
    assert events[0]["kind"] == "confirm"


def test_resume_with_approval_is_accepted():
    client.post("/chat", json={"session_id": "api-4", "message": "帮我签一条登录消息"})
    resp = client.post("/resume", json={"session_id": "api-4", "approved": True})
    events = _events(resp.text)
    assert [e["type"] for e in events] == ["text", "done"]


def test_audit_records_events_without_payload_text():
    client.post("/chat", json={"session_id": "api-5", "message": "看看我的凭证"})
    entries = client.get("/audit").json()
    mine = [e for e in entries if e["session"] == "api-5"]
    assert any(e["event"] == "tool_request" and e["tool"] == "list_credentials" for e in mine)
    # 审计里不该出现消息原文
    assert all("凭证" not in json.dumps(e, ensure_ascii=False) for e in mine)
