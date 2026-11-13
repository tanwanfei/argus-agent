from pydantic import BaseModel, ConfigDict, Field

from argus.kernel.tools import Permission, ToolContext, ToolFn, ToolResult, tool
from argus.knowledge.index import NotesIndex


class NotesSearchArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")

    query: str = Field(
        description=(
            '要找什么。写关键词，用空格隔开，如"外婆 老家"、"第一份工作"；人名、地名照用户说的原样写。'
            '也可以写一句话，如"我小时候最早的记忆"'
        )
    )


def notes_search_tool(index: NotesIndex) -> ToolFn:
    @tool(
        "notes.search",
        Permission.L1,
        params=NotesSearchArgs,
        personal=True,
        sensitive=("query",),
        description=(
            "在用户自己写的资料里查：人生经历（童年、求学、工作、感情）、家里人和家里的情况、"
            "用户对自己和家里人的看法、用户写的随笔。"
            "用户问起自己过去的事、家里的事，问某个人（自己、家里人）什么性格、为人怎么样，要你评价谁，"
            '或者说"我资料里写过的……"时用。'
            "一次最多找回 5 段原文，每段注明出自哪篇；没找到就是资料里没写，不要编"
        ),
    )
    async def search(args: NotesSearchArgs, ctx: ToolContext) -> ToolResult:
        hits = await index.search(args.query)
        if not hits:
            return ToolResult("资料里没有找到相关的内容")
        return ToolResult("\n\n".join(f"出自《{hit.title}》：\n{hit.text}" for hit in hits))

    return search
