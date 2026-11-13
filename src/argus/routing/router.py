from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum

from argus.llm.gateway import CloudHealth
from argus.routing.intents import IntentMatch, IntentTable


class ControlKind(StrEnum):
    CONFIRM = "confirm"
    CANCEL = "cancel"
    UNDO = "undo"
    RECALL = "recall"
    USELESS = "useless"


@dataclass(frozen=True)
class RouteContext:
    awaiting_confirmation: bool = False
    after_proactive_message: bool = False


_CONTROL_PHRASES: dict[ControlKind, frozenset[str]] = {
    ControlKind.CONFIRM: frozenset(
        {
            "确认",
            "确定",
            "确认发送",
            "确认删除",
            "确认忘掉",
            "确认执行",
            "确定删除",
            "确定忘掉",
            "发吧",
            "发送吧",
            "可以发",
            "就这样发",
            "好的发吧",
            "没问题发吧",
            "开始",
            "开始吧",
            "开工",
            "开工吧",
            "可以开始",
        }
    ),
    ControlKind.CANCEL: frozenset(
        {"取消", "算了", "不要了", "不发了", "别发了", "不用了", "不办了"}
    ),
    ControlKind.UNDO: frozenset(
        {
            "撤销",
            "撤销刚才的",
            "撤销一下",
            "刚才的撤销",
            "恢复原样",
            "撤销吧",
            "撤销它",
            "撤销这个",
            "撤销上一步",
        }
    ),
    ControlKind.RECALL: frozenset(
        {"撤回", "撤回刚才的", "撤回一下", "撤回刚才那条", "撤回吧", "撤回它"}
    ),
    ControlKind.USELESS: frozenset({"这条没用", "没用", "这个没用", "这条没什么用", "别推这种了"}),
}


def _applies(kind: ControlKind, context: RouteContext) -> bool:
    match kind:
        case ControlKind.CONFIRM | ControlKind.CANCEL:
            return context.awaiting_confirmation
        case ControlKind.UNDO | ControlKind.RECALL:
            return True
        case ControlKind.USELESS:
            return context.after_proactive_message


@dataclass(frozen=True)
class Control:
    kind: ControlKind


@dataclass(frozen=True)
class RuleHit:
    intent: IntentMatch


@dataclass(frozen=True)
class CloudTurn:
    pass


@dataclass(frozen=True)
class Defer:
    pass


Route = Control | RuleHit | CloudTurn | Defer


class Router:
    def __init__(self, intents: IntentTable, *, cloud_health: Callable[[], CloudHealth]) -> None:
        self._intents = intents
        self._cloud_health = cloud_health

    def route(self, text: str, context: RouteContext, *, heard_wake_word: bool = False) -> Route:
        command = self._intents.command_of(text, heard_wake_word=heard_wake_word)
        for kind, phrases in _CONTROL_PHRASES.items():
            if command in phrases and _applies(kind, context):
                return Control(kind)
        intent = self._intents.match(text, heard_wake_word=heard_wake_word)
        if intent is not None:
            return RuleHit(intent)
        if self._cloud_health() is CloudHealth.DOWN:
            return Defer()
        return CloudTurn()
