import logging
from collections.abc import Awaitable, Callable
from datetime import timedelta

from pydantic import BaseModel, ConfigDict

from argus.kernel.events import CodeTaskFinished, CodeTaskNeedsAttention
from argus.proactive.triggers import Candidate, Trigger, trigger

logger = logging.getLogger(__name__)

_URGENCY = 7
_TTL = timedelta(minutes=30)


class _NoParams(BaseModel):
    model_config = ConfigDict(extra="forbid")


def _say(
    trigger_id: str,
    text: str,
    event: CodeTaskFinished | CodeTaskNeedsAttention,
    *,
    on_phone_already: bool = False,
) -> Candidate:
    return Candidate(
        trigger_id=trigger_id,
        text=text,
        value=8,
        urgency=_URGENCY,
        ttl=_TTL,
        dedupe_key=f"{event.session_id}:{event.ts.isoformat()}",
        personal=False,
        asked_for=True,
        on_phone_already=on_phone_already,
    )


@trigger(on=CodeTaskFinished, id="code_task_finished", params=_NoParams)
def code_task_finished(event: CodeTaskFinished, params: _NoParams) -> Candidate:
    if event.dispatched:
        return _say(
            "code_task_finished",
            dispatched_conclusion(event),
            event,
            on_phone_already=bool(event.details),
        )
    return _say("code_task_finished", f"{event.project} 里的 Claude 做完了。", event)


def dispatched_conclusion(event: CodeTaskFinished) -> str:
    text = _headline(event)
    if event.details:
        text += "详情发到手机了。"
    if not event.finished:
        text += "要它接着做跟我说。"
    return text


def _headline(event: CodeTaskFinished) -> str:
    head = f"派到{event.project}的活" + ("做完了" if event.finished else "停下了，没做完")
    parts = [head]
    if event.changed_files is not None:
        parts.append(f"改了 {event.changed_files} 个文件")
    if event.tests == "没跑":
        parts.append("没跑测试")
    elif event.tests is not None:
        parts.append(f"测试{event.tests}")
    return "，".join(parts) + "。"


class CodeTaskDetails:
    def __init__(self, push_text: Callable[[str, str], Awaitable[bool]]) -> None:
        self._push_text = push_text

    async def on_finished(self, event: CodeTaskFinished) -> None:
        if not event.dispatched or not event.details:
            return
        lines = (_headline(event), event.where, event.details)
        body = "\n".join(line for line in lines if line)
        if not await self._push_text(f"派活：{event.project}", body):
            logger.warning("派活的详情没推到手机，任务 %s", event.session_id)

    async def on_needs_attention(self, event: CodeTaskNeedsAttention) -> None:
        if not event.dispatched or not event.question:
            return
        if not await self._push_text(f"派活：{event.project}有事问你", event.question):
            logger.warning("派活问你的话没推到手机，任务 %s", event.session_id)


@trigger(on=CodeTaskNeedsAttention, id="code_task_needs_attention", params=_NoParams)
def code_task_needs_attention(event: CodeTaskNeedsAttention, params: _NoParams) -> Candidate:
    if event.dispatched:
        text = f"派到{event.project}的活有事问你" + (
            "，问题发到手机了。" if event.question else "。"
        )
        return _say("code_task_needs_attention", text, event, on_phone_already=bool(event.question))
    return _say("code_task_needs_attention", f"{event.project} 里的 Claude 在等你批准。", event)


CODE_LISTENING_TRIGGERS: tuple[Trigger, ...] = (code_task_finished, code_task_needs_attention)
