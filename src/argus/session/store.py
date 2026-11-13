import json
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

from argus.kernel.tools import Speaker, ToolCall
from argus.llm.gateway import Role


@dataclass(frozen=True)
class WorkingMessage:
    ts: datetime
    trace_id: str
    role: Role
    speaker: Speaker
    content: str
    tool_call: ToolCall | None = None

    @property
    def searchable_text(self) -> str:
        if self.tool_call is None:
            return self.content
        return self.content + tool_call_to_json(self.tool_call)


def tool_call_to_json(call: ToolCall) -> str:
    return json.dumps(
        {"id": call.id, "name": call.name, "args": dict(call.args)}, ensure_ascii=False
    )


def tool_call_from_json(text: str) -> ToolCall:
    stored = json.loads(text)
    return ToolCall(stored["id"], stored["name"], stored["args"])


def mentions_topic(topic: str, content: str) -> bool:
    normalized_topic = topic.strip().casefold()
    return bool(normalized_topic) and normalized_topic in content.casefold()


class SessionStore(Protocol):
    async def append(
        self, messages: Sequence[WorkingMessage], *, expired_before: datetime
    ) -> None: ...

    async def load(self, *, since: datetime) -> list[WorkingMessage]: ...

    async def forget(self, topic: str) -> None: ...
