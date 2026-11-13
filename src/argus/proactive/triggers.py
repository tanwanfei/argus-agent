import logging
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import timedelta
from typing import Any, Self

from pydantic import BaseModel, ValidationError

from argus.kernel.events import Event

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class Candidate:
    trigger_id: str
    text: str
    value: int
    urgency: int
    ttl: timedelta
    dedupe_key: str
    personal: bool = True
    asked_for: bool = False
    on_phone_already: bool = False

    def __post_init__(self) -> None:
        for name, score in (("value", self.value), ("urgency", self.urgency)):
            if not 0 <= score <= 10:
                raise ValueError(f"触发器 {self.trigger_id} 的 {name} 是 {score}，要在 0～10 之间")


@dataclass(frozen=True)
class Trigger:
    id: str
    on: type[Event]
    params: type[BaseModel]
    run: Callable[[Any, Any], Candidate | None]


def trigger[EventT: Event, ParamsT: BaseModel](
    *, on: type[EventT], id: str, params: type[ParamsT]
) -> Callable[[Callable[[EventT, ParamsT], Candidate | None]], Trigger]:
    def bind(run: Callable[[EventT, ParamsT], Candidate | None]) -> Trigger:
        return Trigger(id=id, on=on, params=params, run=run)

    return bind


@dataclass(frozen=True)
class TriggerSetting:
    enabled: bool
    params: Mapping[str, object]


@dataclass(frozen=True)
class _EnabledTrigger:
    trigger: Trigger
    params: BaseModel


class EnabledTriggers:
    def __init__(self, enabled: Sequence[_EnabledTrigger]) -> None:
        self._enabled = tuple(enabled)

    @classmethod
    def build(cls, available: Sequence[Trigger], settings: Mapping[str, TriggerSetting]) -> Self:
        known_ids = {available_trigger.id for available_trigger in available}
        for trigger_id in settings:
            if trigger_id not in known_ids:
                logger.error("triggers.yaml 里写的触发器 %s 不存在，这条不认", trigger_id)

        enabled = []
        for available_trigger in available:
            setting = settings.get(available_trigger.id)
            if setting is None or not setting.enabled:
                continue
            try:
                params = available_trigger.params.model_validate(setting.params)
            except ValidationError as error:
                logger.error(
                    "triggers.yaml 里触发器 %s 的参数不对，这次不开：%s",
                    available_trigger.id,
                    error,
                )
                continue
            enabled.append(_EnabledTrigger(available_trigger, params))

        enabled_ids = "、".join(entry.trigger.id for entry in enabled) or "没有"
        logger.info("开着的主动触发器：%s", enabled_ids)
        return cls(enabled)

    def event_types(self) -> set[type[Event]]:
        return {entry.trigger.on for entry in self._enabled}

    def fire(self, event: Event) -> list[Candidate]:
        candidates = []
        for entry in self._enabled:
            if type(event) is not entry.trigger.on:
                continue
            try:
                candidate = entry.trigger.run(event, entry.params)
            except Exception:
                logger.exception("触发器 %s 出错，这次跳过", entry.trigger.id)
                continue
            if candidate is not None:
                candidates.append(candidate)
        return candidates
