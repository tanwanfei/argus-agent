import logging
from collections.abc import Sequence

from argus.cognition.brain import TurnInput
from argus.llm.gateway import Message, Role
from argus.memory.memory import Memory
from argus.memory.records import MemoryItem

logger = logging.getLogger(__name__)

RECALL_HEADER = "你记得的相关内容（来自长期记忆）："
RESIDENT_HEADER = "你已经知道的关于用户的事（来自长期记忆）："


class MemoryContext:
    def __init__(self, memory: Memory) -> None:
        self._memory = memory

    async def resident_messages(self) -> Sequence[Message]:
        try:
            items = await self._memory.resident()
        except Exception:
            logger.exception("取常驻记忆出错，这一轮不带")
            return ()
        if not items:
            return ()
        return (Message(Role.SYSTEM, _with_header(RESIDENT_HEADER, items)),)

    async def recall(self, turn: TurnInput) -> Sequence[Message]:
        try:
            items = await self._memory.recall(turn.utterance)
            already_known = {item.text for item in await self._memory.resident()}
        except Exception:
            logger.exception("召回记忆出错，这一轮不带记忆")
            return ()
        items = [item for item in items if item.text not in already_known]
        if not items:
            return ()
        return (Message(Role.SYSTEM, _with_header(RECALL_HEADER, items)),)


def _with_header(header: str, items: Sequence[MemoryItem]) -> str:
    lines = "\n".join(f"- {item.text}" for item in items)
    return f"{header}\n{lines}"
