import logging
from collections.abc import Mapping
from pathlib import PureWindowsPath

from argus.kernel.clock import Clock
from argus.kernel.events import CodeTaskFinished, CodeTaskNeedsAttention, EventBus

logger = logging.getLogger(__name__)

_PERMISSION_PROMPT = "permission_prompt"


class HookRejected(ValueError):
    pass


class ClaudeCodeHooks:
    def __init__(self, bus: EventBus, clock: Clock) -> None:
        self._bus = bus
        self._clock = clock

    def on_hook(self, payload: Mapping[str, object]) -> None:
        event_name = payload.get("hook_event_name")
        if not isinstance(event_name, str):
            raise HookRejected("缺 hook_event_name")

        if event_name == "Stop":
            project, session_id = _project_and_session_of(payload)
            self._bus.publish(
                CodeTaskFinished(ts=self._clock.now(), project=project, session_id=session_id)
            )
            logger.info("Claude Code 旁听：%s 答完了", project)
        elif (
            event_name == "Notification" and payload.get("notification_type") == _PERMISSION_PROMPT
        ):
            project, session_id = _project_and_session_of(payload)
            self._bus.publish(
                CodeTaskNeedsAttention(ts=self._clock.now(), project=project, session_id=session_id)
            )
            logger.info("Claude Code 旁听：%s 在等你批准", project)
        else:
            logger.info("Claude Code 旁听：收到 %s，不提醒", event_name)


def _project_and_session_of(payload: Mapping[str, object]) -> tuple[str, str]:
    session_id = payload.get("session_id")
    if not isinstance(session_id, str) or not session_id:
        raise HookRejected("缺 session_id")
    working_directory = payload.get("cwd")
    if not isinstance(working_directory, str) or not working_directory:
        raise HookRejected("缺 cwd")
    return _project_name(working_directory), session_id


def _project_name(working_directory: str) -> str:
    return PureWindowsPath(working_directory).name or working_directory
