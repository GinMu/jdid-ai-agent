"""统一日志:把"工具调用参数"和"工具返回结果"同时打到控制台与本地文件。

只打**已经过策略层**的数据:参数是 `redact()` 之后的,结果是钱包回传的摘要
(执行器只挑 id/类型/发行方/过期时间),因此日志里不会出现私钥或凭证原文。
结果**原样输出、不截断**(需要完整上下文时可直接看日志文件)。

文件默认写到 `logs/did-ai-agent.log`,按大小轮转(`RotatingFileHandler`),
路径可用 `DID_AI_LOG_FILE` 覆盖;`DID_AI_LOG_LEVEL` 控制级别。
"""

import json
import logging
import os
from logging.handlers import RotatingFileHandler
from pathlib import Path

LOGGER_NAME = "did_ai_agent"
DEFAULT_LOG_FILE = "logs/did-ai-agent.log"
MAX_BYTES = 5 * 1024 * 1024
BACKUP_COUNT = 3

logger = logging.getLogger(LOGGER_NAME)

_FORMAT = "%(asctime)s %(levelname)s %(name)s %(message)s"


def configure(level: int | None = None, log_file: str | None = None) -> Path:
    """配置日志(幂等)。

    @return 实际写入的日志文件路径(便于启动时打印一行,告诉使用者去哪看)。
    """
    resolved_level = level if level is not None else _level_from_env()
    path = Path(log_file or os.environ.get("DID_AI_LOG_FILE", DEFAULT_LOG_FILE))
    path.parent.mkdir(parents=True, exist_ok=True)

    logger.setLevel(resolved_level)
    logger.propagate = False
    formatter = logging.Formatter(_FORMAT)

    # 换路径/重复配置时,先摘掉旧的写文件 handler(测试会多次调用 configure)
    for handler in list(logger.handlers):
        if isinstance(handler, RotatingFileHandler):
            logger.removeHandler(handler)
            handler.close()
    if not any(
        isinstance(h, logging.StreamHandler) and not isinstance(h, RotatingFileHandler)
        for h in logger.handlers
    ):
        console = logging.StreamHandler()
        console.setFormatter(formatter)
        logger.addHandler(console)

    file_handler = RotatingFileHandler(
        path, maxBytes=MAX_BYTES, backupCount=BACKUP_COUNT, encoding="utf-8"
    )
    file_handler.setFormatter(formatter)
    logger.addHandler(file_handler)
    logger.info("logging to %s", path)
    return path


def _level_from_env() -> int:
    raw = os.environ.get("DID_AI_LOG_LEVEL", "INFO").upper()
    return getattr(logging, raw, logging.INFO)


def _render(value: object) -> str:
    """序列化成一行文本(不截断:排查时需要完整参数/结果)。"""
    try:
        return json.dumps(value, ensure_ascii=False)
    except (TypeError, ValueError):
        return str(value)


def log_tool_request(session_id: str, tool: str, args: object, kind: str) -> None:
    logger.info(
        "tool_request session=%s tool=%s kind=%s args=%s", session_id, tool, kind, _render(args)
    )


def log_tool_result(session_id: str, tool: str, result: object) -> None:
    logger.info("tool_result  session=%s tool=%s result=%s", session_id, tool, _render(result))


def log_resume(session_id: str, decision: object) -> None:
    """记录钱包回传的 resume 内容(工具结果 / 用户决定),完整输出、不截断。"""
    logger.info("resume       session=%s decision=%s", session_id, _render(decision))


def log_chat(session_id: str, chars: int) -> None:
    logger.info("chat         session=%s chars=%d", session_id, chars)
