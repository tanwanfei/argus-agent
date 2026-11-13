from pydantic import BaseModel, ConfigDict, Field

from argus.kernel.tools import (
    Permission,
    Speaker,
    ToolContext,
    ToolFailure,
    ToolFn,
    ToolResult,
    tool,
)
from argus.situation.others_present import OthersPresence


class GuestsArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")

    present: bool = Field(
        description='true 是来了客人（"有客人"），false 是客人都走了（"客人走了"）'
    )


def guests_tool(others: OthersPresence) -> ToolFn:
    @tool(
        "guests.set",
        Permission.L1,
        params=GuestsArgs,
        timeout_s=1,
        personal=False,
        description=(
            "告诉管家家里现在有没有客人。有客人时，用户的私事提醒不在家里的音箱念，改发手机；"
            "到第二天早上 6 点自动算客人走了。"
            '用户说家里来人了（"我朋友来家里了""一会儿有人来吃饭，现在到了"）时 present=true，'
            '说客人都走了（"人都走了""朋友回去了"）时 present=false。只是聊到客人、没说现在来了走了时不用'
        ),
    )
    async def set_guests(args: GuestsArgs, ctx: ToolContext) -> ToolResult:
        if args.present:
            others.guests_arrived()
            return ToolResult("记下了：家里有客人，到早上 6 点为止")
        if ctx.speaker is not Speaker.OWNER:
            raise ToolFailure("没认出是你的声音", user_action="客人走了要你本人来说")
        others.guests_left()
        return ToolResult("记下了：客人走了")

    return set_guests
