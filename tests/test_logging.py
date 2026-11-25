"""日志:工具请求/结果要落到本地文件,且敏感字段不出现。"""

import logging

from did_ai_agent import logging_setup


def test_logs_are_written_to_the_file(tmp_path):
    log_file = tmp_path / "agent.log"
    logging_setup.configure(level=logging.INFO, log_file=str(log_file))

    logging_setup.log_tool_request("s1", "list_credentials", {"limit": 5}, "read")
    logging_setup.log_tool_result("s1", "list_credentials", {"total": 7})
    logging_setup.log_chat("s1", 12)
    for handler in logging_setup.logger.handlers:
        handler.flush()

    text = log_file.read_text(encoding="utf-8")
    assert 'tool_request session=s1 tool=list_credentials kind=read args={"limit": 5}' in text
    assert 'tool_result  session=s1 tool=list_credentials result={"total": 7}' in text
    assert "chat         session=s1 chars=12" in text


def test_long_results_are_written_in_full(tmp_path):
    """不截断:完整结果都要能落到日志里(排查时需要看到全量)。"""
    log_file = tmp_path / "agent.log"
    logging_setup.configure(level=logging.INFO, log_file=str(log_file))

    long_value = "x" * 2000
    logging_setup.log_tool_result("s1", "list_credentials", {"items": [long_value]})
    for handler in logging_setup.logger.handlers:
        handler.flush()

    text = log_file.read_text(encoding="utf-8")
    assert long_value in text, "2000 字符的结果应完整写入"
    assert "…(共" not in text, "不应出现截断标记"


def test_resume_content_is_logged_in_full(tmp_path):
    log_file = tmp_path / "agent.log"
    logging_setup.configure(level=logging.INFO, log_file=str(log_file))

    decision = {"result": {"total": 7, "items": [{"id": "a#1"}, {"id": "a#2"}]}}
    logging_setup.log_resume("s1", decision)
    for handler in logging_setup.logger.handlers:
        handler.flush()

    text = log_file.read_text(encoding="utf-8")
    assert 'resume       session=s1 decision={"result": {"total": 7' in text
    assert '"a#2"' in text
