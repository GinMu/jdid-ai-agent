"""本地审计:只记"谁、何时、哪个工具、数据类别",不记原文。"""

from collections import deque
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any


@dataclass
class AuditEntry:
    at: str
    session_id: str
    event: str
    tool: str | None = None
    detail: dict[str, Any] = field(default_factory=dict)


class AuditLog:
    def __init__(self, size: int = 200) -> None:
        self._entries: deque[AuditEntry] = deque(maxlen=size)

    def record(
        self,
        session_id: str,
        event: str,
        *,
        tool: str | None = None,
        detail: dict[str, Any] | None = None,
    ) -> AuditEntry:
        entry = AuditEntry(
            at=datetime.now(UTC).isoformat(timespec="seconds"),
            session_id=session_id,
            event=event,
            tool=tool,
            detail=detail or {},
        )
        self._entries.append(entry)
        return entry

    def entries(self) -> list[AuditEntry]:
        return list(self._entries)

    def clear(self) -> None:
        self._entries.clear()
