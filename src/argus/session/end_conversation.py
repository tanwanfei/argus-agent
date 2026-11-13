from pydantic import BaseModel, ConfigDict

from argus.kernel.tools import Permission, ToolContext, ToolFn, ToolResult, tool

END_CONVERSATION_TOOL = "conversation.end"


class NoArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")


def end_conversation_tool() -> ToolFn:
    @tool(
        END_CONVERSATION_TOOL,
        Permission.L1,
        params=NoArgs,
        timeout_s=1,
        personal=False,
        description=(
            "结束这次对话：之后用户再说话要先喊你的名字，你才会听。用户明确要你别说了、别听了、"
            '这次没事了时用，比如"好了好了别说了""行了你闭嘴吧""没事了你退下吧""我不跟你说了"。'
            '只是道谢、说"好的"时不用。调了以后只回一个"好"字，或者什么都不说'
        ),
    )
    async def end_conversation(args: NoArgs, ctx: ToolContext) -> ToolResult:
        return ToolResult("对话结束了：用户再说话要先喊名字")

    return end_conversation
