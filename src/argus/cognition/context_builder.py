from collections.abc import Sequence

from argus.kernel.tools import Speaker
from argus.llm.gateway import ChatMessage, Message, Role

_SPOKEN_NOTE = "用户是说的，回复会念出来"
_TYPED_NOTE = "用户是打字发来的，回复显示成文字"


def build_context(
    *,
    persona: str,
    situation: str,
    history: Sequence[ChatMessage],
    utterance: str,
    spoken: bool,
    speaker: Speaker,
    resident_memory: Sequence[Message] = (),
    recalled_memory: Sequence[Message] = (),
) -> tuple[ChatMessage, ...]:
    memory = (*resident_memory, *recalled_memory) if speaker is Speaker.OWNER else ()
    messages: list[ChatMessage] = [Message(Role.SYSTEM, persona), *memory, *history]
    reply_note = _SPOKEN_NOTE if spoken else _TYPED_NOTE
    if situation:
        prefix = f"（当前情境：{situation}；{reply_note}）"
    else:
        prefix = f"（{reply_note}）"
    messages.append(Message(Role.USER, prefix + utterance))
    return tuple(messages)
