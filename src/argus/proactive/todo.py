from dataclasses import dataclass
from datetime import datetime
from typing import Annotated, Protocol

from pydantic import BaseModel, ConfigDict, Field, StringConstraints

from argus.kernel.clock import Clock
from argus.kernel.tools import Permission, ToolContext, ToolFailure, ToolFn, ToolResult, tool

_MOST_LISTED = 50
_MOST_SHOWN_IN_FAILURE = 10
_TIMEOUT_S = 5

_Title = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=200)]


@dataclass(frozen=True)
class TodoItem:
    id: int
    title: str
    created_at: datetime


class TodoList(Protocol):
    async def add(self, title: str, created_at: datetime) -> TodoItem: ...

    async def pending(self) -> list[TodoItem]: ...

    async def mark_done(self, item_id: int, done_at: datetime) -> bool: ...

    async def reopen(self, item_id: int) -> bool: ...

    async def remove(self, item_id: int) -> bool: ...


class NoArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")


class AddTodoArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")

    title: _Title


class DoneTodoArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")

    keyword: _Title


class ItemArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")

    item_id: int


class ItemIdsArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")

    item_ids: Annotated[list[int], Field(min_length=1)]


@dataclass(frozen=True)
class TodoTools:
    list_all: ToolFn
    add: ToolFn
    remove: ToolFn
    done: ToolFn
    reopen: ToolFn


def todo_tools(todos: TodoList, clock: Clock) -> TodoTools:
    @tool(
        "todo.list",
        Permission.L1,
        params=NoArgs,
        timeout_s=_TIMEOUT_S,
        description='查用户还没做的待办。用户问"我有什么待办""还有什么事没做"时用。已经划掉的不列',
    )
    async def list_todos(args: NoArgs, ctx: ToolContext) -> ToolResult:
        pending = await todos.pending()
        if not pending:
            return ToolResult("待办清单是空的，没有没做的事")
        lines = [f"没做的待办共 {len(pending)} 项："]
        for item in pending[:_MOST_LISTED]:
            created = item.created_at.astimezone(clock.now().tzinfo)
            lines.append(f"- {item.title}（{created.month}月{created.day}日记下）")
        if len(pending) > _MOST_LISTED:
            lines.append(f"还有 {len(pending) - _MOST_LISTED} 项没列出")
        return ToolResult("\n".join(lines))

    @tool(
        "todo.add",
        Permission.L2,
        params=AddTodoArgs,
        timeout_s=_TIMEOUT_S,
        sensitive=("title",),
        description=(
            '往待办清单里记一件事。用户说"加个待办，买牛奶""记一下要交报告"时用。'
            "这是没有固定时刻的事；到点要提醒的用闹钟"
        ),
    )
    async def add_todo(args: AddTodoArgs, ctx: ToolContext) -> ToolResult:
        wanted = args.title.casefold()
        existing = next(
            (item for item in await todos.pending() if item.title.casefold() == wanted), None
        )
        if existing is not None:
            return ToolResult(f"清单里已经有这条待办了：{existing.title}，没再记一遍")
        item = await todos.add(args.title, clock.now())
        return ToolResult(f"记下了待办：{item.title}", undo_args={"item_id": item.id})

    @tool(
        "todo.remove",
        Permission.L2,
        params=ItemArgs,
        timeout_s=_TIMEOUT_S,
        description="撤销刚记的待办：从清单里删掉（撤销用，不给模型）",
    )
    async def remove_todo(args: ItemArgs, ctx: ToolContext) -> ToolResult:
        if await todos.remove(args.item_id):
            return ToolResult("那条待办删掉了")
        return ToolResult("那条待办已经不在清单里了")

    @tool(
        "todo.done",
        Permission.L2,
        params=DoneTodoArgs,
        timeout_s=_TIMEOUT_S,
        sensitive=("keyword",),
        description=(
            "把一条待办划掉（做完了）。keyword 是那条待办标题里的几个字，用用户说的词，"
            "不要自己编；有好几条都包含时会报错，让用户说得具体些"
        ),
    )
    async def done_todo(args: DoneTodoArgs, ctx: ToolContext) -> ToolResult:
        same = _find_same(await todos.pending(), args.keyword)
        now = clock.now()
        crossed_out = [item for item in same if await todos.mark_done(item.id, now)]
        if not crossed_out:
            raise ToolFailure(f"待办「{same[0].title}」已经划掉了", user_action=None)
        text = f"划掉了：{crossed_out[0].title}"
        if len(crossed_out) > 1:
            text += f"（记重了 {len(crossed_out)} 条，一起划掉了）"
        return ToolResult(text, undo_args={"item_ids": [item.id for item in crossed_out]})

    @tool(
        "todo.reopen",
        Permission.L2,
        params=ItemIdsArgs,
        timeout_s=_TIMEOUT_S,
        description="撤销刚划掉的待办：改回没做（撤销用，不给模型）",
    )
    async def reopen_todo(args: ItemIdsArgs, ctx: ToolContext) -> ToolResult:
        reopened = [item_id for item_id in args.item_ids if await todos.reopen(item_id)]
        if reopened:
            return ToolResult("那条待办改回没做了")
        return ToolResult("那条待办已经是没做的了")

    return TodoTools(
        list_all=list_todos, add=add_todo, remove=remove_todo, done=done_todo, reopen=reopen_todo
    )


def _find_same(pending: list[TodoItem], keyword: str) -> list[TodoItem]:
    wanted = keyword.casefold()
    exact = [item for item in pending if item.title.casefold() == wanted]
    containing = exact or [item for item in pending if wanted in item.title.casefold()]
    if len({item.title.casefold() for item in containing}) == 1:
        return containing
    if not containing:
        raise ToolFailure(f"待办里没有包含「{keyword}」的。{_current(pending)}", user_action=None)
    titles = "、".join(item.title for item in containing[:_MOST_SHOWN_IN_FAILURE])
    raise ToolFailure(
        f"有好几条待办都包含「{keyword}」：{titles}，不知道划哪一条", user_action=None
    )


def _current(pending: list[TodoItem]) -> str:
    if not pending:
        return "清单是空的"
    titles = "、".join(item.title for item in pending[:_MOST_SHOWN_IN_FAILURE])
    return f"现在没做的有：{titles}"
