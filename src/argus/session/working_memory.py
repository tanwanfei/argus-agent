from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta

from argus.kernel.tools import Speaker
from argus.llm.gateway import ChatMessage, Message, Role, ToolCallsMessage, ToolResultMessage
from argus.session.store import WorkingMessage, mentions_topic

WINDOW = timedelta(hours=2)
MAX_TURNS = 20

_CUT_OFF_MARK = "（下面这句念到一半被打断，用户没听全：{sentence}）"
_INTERRUPTED_MARK = "（念到这里被打断了）"
_NOT_STARTED_MARK = "（还没开口就被打断了）"
_TICKET_OPENED = (
    "开了确认单，还没执行。后面那句确认的话是主机替你说的。"
    "用户说「确认」「开始」这类话，或者在手机卡片上批准（主机说了「需要你在手机上确认」的才有卡片），"
    "由主机直接去办，不经过你；交到你这里的话都不是确认，你也确认不了。"
    "所以用户回你「可以」「好」这类话时，不能说已经办了、派出去了："
    "有卡片的让用户去手机上批（说「确认」对它不管用），没有卡片的让用户说「确认」（派活也可以说「开始」）。"
    "单子 2 分钟后作废，作废了再要办得重新调工具"
)


def reply_to_remember(
    finished: Sequence[str],
    cut_off: str | None,
    *,
    interrupted: bool,
    from_tools: Sequence[str] = (),
) -> str | None:
    parts = [_what_was_said(finished, cut_off, interrupted=interrupted), *from_tools]
    kept = [part for part in parts if part]
    return "\n\n".join(kept) if kept else None


def _what_was_said(
    finished: Sequence[str], cut_off: str | None, *, interrupted: bool
) -> str | None:
    said = "".join(finished)
    if not interrupted:
        return said or None
    if cut_off is not None:
        return said + _CUT_OFF_MARK.format(sentence=cut_off)
    if said:
        return said + _INTERRUPTED_MARK
    return _NOT_STARTED_MARK


@dataclass(frozen=True)
class _Turn:
    utterance: WorkingMessage
    reply: WorkingMessage


class WorkingMemory:
    def __init__(self) -> None:
        self._turns: list[_Turn] = []

    def add_turn(self, utterance: WorkingMessage, reply: WorkingMessage) -> None:
        self._turns.append(_Turn(utterance, reply))
        del self._turns[:-MAX_TURNS]

    def restore(self, messages: Sequence[WorkingMessage]) -> None:
        restored: list[_Turn] = []
        for index in range(0, len(messages), 2):
            pair = messages[index : index + 2]
            is_turn = (
                len(pair) == 2
                and pair[0].role == Role.USER
                and pair[1].role == Role.ASSISTANT
                and pair[0].trace_id == pair[1].trace_id
                and pair[0].speaker == pair[1].speaker
            )
            if not is_turn:
                raise ValueError(f"ses_message 读回来的第 {index + 1} 条起配不成一轮")
            restored.append(_Turn(utterance=pair[0], reply=pair[1]))
        for turn in restored:
            self.add_turn(turn.utterance, turn.reply)

    def forget(self, topic: str) -> int:
        kept = [
            turn
            for turn in self._turns
            if not (
                mentions_topic(topic, turn.utterance.searchable_text)
                or mentions_topic(topic, turn.reply.searchable_text)
            )
        ]
        removed_count = len(self._turns) - len(kept)
        self._turns = kept
        return removed_count

    def history(self, now: datetime, speaker: Speaker) -> tuple[ChatMessage, ...]:
        cutoff = now - WINDOW
        recent_turns = [
            turn
            for turn in self._turns
            if turn.utterance.ts >= cutoff
            and (speaker is Speaker.OWNER or turn.utterance.speaker is speaker)
        ]
        return tuple(message for turn in recent_turns for message in _as_it_happened(turn))


def _as_it_happened(turn: _Turn) -> tuple[ChatMessage, ...]:
    utterance = Message(turn.utterance.role, turn.utterance.content)
    reply = Message(turn.reply.role, turn.reply.content)
    call = turn.reply.tool_call
    if call is None:
        return (utterance, reply)
    return (
        utterance,
        ToolCallsMessage("", (call,)),
        ToolResultMessage(call.id, _TICKET_OPENED),
        reply,
    )
