from pydantic import BaseModel, ConfigDict, Field

from argus.delivery.delivery import Delivery
from argus.kernel.tools import Permission, ToolContext, ToolFn, ToolResult, tool

_REMEMBER_MARK = "（发到手机的全文：\n{text}）"


class SendTextArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")

    text: str = Field(min_length=1, description='要发的全文，可以分段、用"- "开头的简短列表')


def send_to_phone_tool(delivery: Delivery, *, phone_endpoint_id: str) -> ToolFn:
    @tool(
        "phone.send_text",
        Permission.L1,
        params=SendTextArgs,
        timeout_s=15,
        sensitive=("text",),
        personal=True,
        description=(
            "把一段带格式的长内容（步骤、清单、菜谱、总结等）发到用户手机上的阿格斯 App，"
            "用户在文字页看。用户是说的、要给的内容多时用它，口头只说一两句"
        ),
    )
    async def send_text(args: SendTextArgs, ctx: ToolContext) -> ToolResult:
        remember = _REMEMBER_MARK.format(text=args.text)
        if await delivery.send_text(phone_endpoint_id, args.text):
            return ToolResult("已经发到用户手机上的 App 了", remember=remember)
        await delivery.remind_reply_not_delivered(args.text)
        return ToolResult(
            "用户手机上的 App 现在没连着，已经推了通知提醒，用户打开 App 能看到", remember=remember
        )

    return send_text
