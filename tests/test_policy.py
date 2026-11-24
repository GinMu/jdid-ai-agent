import pytest

from did_ai_agent.policy import PolicyError, RateLimiter, redact, validate_call


def test_unknown_tool_is_rejected():
    with pytest.raises(PolicyError, match="未知工具"):
        validate_call("delete_everything", {})


def test_missing_required_arg_is_rejected():
    with pytest.raises(PolicyError, match="缺少必填参数"):
        validate_call("resolve_did", {})


def test_wrong_type_is_rejected():
    with pytest.raises(PolicyError, match="类型应为"):
        validate_call("list_credentials", {"limit": "many"})


def test_extra_arg_is_rejected():
    with pytest.raises(PolicyError, match="不接受参数"):
        validate_call("get_identity", {"address": "j..."})


def test_valid_call_passes_through():
    assert validate_call("list_credentials", {"limit": 5}) == {"limit": 5}
    assert validate_call("get_identity", None) == {}


def test_redact_drops_sensitive_keys_recursively():
    payload = {
        "a": 1,
        "privateKey": "x",
        "nested": {"mnemonic": "y", "b": 2},
        "list": [{"secret": 3}],
    }
    assert redact(payload) == {
        "a": 1,
        "privateKey": "***",
        "nested": {"mnemonic": "***", "b": 2},
        "list": [{"secret": "***"}],
    }


def test_rate_limiter_blocks_after_quota():
    limiter = RateLimiter(per_minute=2)
    limiter.check("s1")
    limiter.check("s1")
    with pytest.raises(PolicyError, match="过于频繁"):
        limiter.check("s1")
    limiter.check("s2")  # 其它会话不受影响
