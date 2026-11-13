from pydantic import BaseModel, ConfigDict, Field, field_validator

from argus.kernel.clock import Clock
from argus.kernel.events import EventBus, MemoryForgotten
from argus.kernel.tools import Permission, ToolContext, ToolFailure, ToolFn, ToolResult, tool
from argus.memory.matching import facts_about
from argus.memory.memory import Memory


class RememberArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")

    subject: str = Field(
        default="我",
        description='说的是谁或什么。用户说自己时写"我"，说别人写称呼，如"妈妈"、"豆豆"',
    )
    attribute: str = Field(description='哪一方面，尽量短，如"过敏"、"咖啡"、"生日"')
    value: str = Field(description='具体内容，如"花生"、"美式"、"三月八号"')
    replaces: bool = Field(
        default=False,
        description=(
            '用户是在更正或替换以前记过的内容（说"改成""不再""换成"）时为 true，旧的会被取代；'
            "只是再补充一条（如又对另一样东西过敏）时为 false，新旧并存"
        ),
    )


class UnrememberArgs(BaseModel):
    fact_id: int


class SearchArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")

    query: str = Field(
        default="",
        description='要找什么，如"过敏"、"妈妈的生日"、"咖啡"；用户问记住了什么、想看全部时留空',
    )


def remember_tools(memory: Memory) -> tuple[ToolFn, ToolFn]:
    @tool(
        "memory.remember",
        Permission.L2,
        params=RememberArgs,
        personal=True,
        sensitive=("subject", "attribute", "value"),
        description=(
            "把一件关于用户的事记到长期记忆里，以后的对话想得起来。"
            "用户明确说要记住什么时用，或者用户告诉你家人、口味、过敏、作息、重要日期这类稳定的信息、"
            '并希望你记着时用。拆成"主体·属性·值"，如"记住我对花生过敏"→主体"我"、属性"过敏"、值"花生"。'
            "闲聊、一次性的事不要记。工具成功了才能跟用户说记下了"
        ),
    )
    async def remember(args: RememberArgs, ctx: ToolContext) -> ToolResult:
        try:
            outcome = await memory.remember(
                subject=args.subject,
                attribute=args.attribute,
                value=args.value,
                replaces=args.replaces,
            )
        except ValueError as error:
            raise ToolFailure(f"没记成：{error}", user_action=None) from error
        if outcome.fact is None:
            return ToolResult(f"这条已经记着了：{args.subject}的{args.attribute}是{args.value}")
        text = f"记下了：{args.subject}的{args.attribute}是{args.value}"
        if outcome.replaced:
            old_values = "、".join(fact.value for fact in outcome.replaced)
            text += f"（取代了原来记的：{old_values}）"
        return ToolResult(text, undo_args={"fact_id": outcome.fact.id})

    @tool(
        "memory.unremember",
        Permission.L2,
        params=UnrememberArgs,
        personal=True,
        description="撤销刚记下的那条记忆，被它取代的旧记忆恢复（撤销用，不给模型）",
    )
    async def unremember(args: UnrememberArgs, ctx: ToolContext) -> ToolResult:
        if await memory.undo_remember(args.fact_id):
            return ToolResult("那条记忆删掉了")
        return ToolResult("那条记忆已经不在了")

    return remember, unremember


def search_tool(memory: Memory) -> ToolFn:
    @tool(
        "memory.search",
        Permission.L1,
        params=SearchArgs,
        personal=True,
        sensitive=("query",),
        description=(
            "在长期记忆里查关于用户的事：家人、口味、过敏、作息、重要日期这类用户让你记过的稳定信息。"
            '用户问"我对什么过敏""你还记得我……吗""你都记了我什么"这类话时用。现在只查用户明确让记、'
            "或已经整理好的事实，查不到不代表没发生过"
        ),
    )
    async def search(args: SearchArgs, ctx: ToolContext) -> ToolResult:
        items = await memory.search(args.query)
        if not items:
            return ToolResult("长期记忆里没有找到相关的内容")
        return ToolResult("\n".join(f"- {item.text}" for item in items))

    return search


class ForgetArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")

    topic: str = Field(
        description=(
            "要忘掉的主题，至少两个字，用用户说的那个词原样写，不要扩写、不要换说法："
            '用户说"忘掉花生"就写"花生"，不要写成"花生过敏"。'
            "删的是主体、属性、值里包含这个词的记忆（连以前的记录），写长了反而一条都对不上"
        )
    )

    @field_validator("topic")
    @classmethod
    def _specific_enough(cls, topic: str) -> str:
        facts_about(topic, [])
        return topic.strip()


def _confirm_forget(args: ForgetArgs) -> str:
    return f"忘掉所有跟{args.topic}有关的记忆，删了就找不回来。要删就说确认删除。"


def forget_tool(memory: Memory, *, events: EventBus, clock: Clock) -> ToolFn:
    @tool(
        "memory.forget",
        Permission.L3A,
        params=ForgetArgs,
        personal=True,
        confirm=_confirm_forget,
        sensitive=("topic",),
        description=(
            '把长期记忆里跟某个主题有关的内容彻底删掉，删了找不回来。用户说"忘掉""别再记着"某件事时调用它。'
            "确认由系统来问：调用以后系统会念出要删什么、等用户本人说确认才删，你不要自己问确认，"
            "也不要不调用就说已经忘了，这个工具没调用成功之前什么都没删。"
            "用户只想改一个值时不要用它，用 memory.remember 的 replaces"
        ),
    )
    async def forget(args: ForgetArgs, ctx: ToolContext) -> ToolResult:
        try:
            outcome = await memory.forget(args.topic)
        except ValueError as error:
            raise ToolFailure(f"没忘成：{error}", user_action=None) from error
        events.publish(MemoryForgotten(ts=clock.now(), topic=args.topic))
        if outcome.facts_deleted == 0 and outcome.episodes_deleted == 0:
            raise ToolFailure(
                f"长期记忆里没有跟{args.topic}有关的内容，没有删什么",
                user_action="换个更短的说法再试，比如只说关键的那个词",
            )
        return ToolResult(
            f"已经忘掉了：{outcome.facts_deleted} 条事实、{outcome.episodes_deleted} 件经历"
        )

    return forget
