from pydantic import BaseModel, ConfigDict, Field

from argus.codeagent.agent import CodeAgent, DispatchRefused, LiveProgress
from argus.codeagent.tasks import CodeTask, TaskStatus
from argus.kernel.tools import Permission, ToolContext, ToolFailure, ToolFn, ToolResult, tool

_DISPATCH_TIMEOUT_S = 120


class DispatchArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")

    project: str = Field(title="目录", description="派活的目录，只能是工具说明里列出的名字之一")
    request: str = Field(
        title="要做的",
        min_length=1,
        description="要代理做的事，一句话，照用户的原话，不要扩写、不要替用户加细节，如：把登录页点提交时的报错修好",
    )


DISPATCHED_REPLY = "派出去了，做完告诉你。"
PUBLISHED_REPLY = "写回去了：{where}。"


def dispatch_tool(agent: CodeAgent) -> ToolFn:
    names = "、".join(agent.project_names)

    def confirm(args: DispatchArgs) -> str:
        request = args.request.rstrip("。.！!；; ")
        return f"让代理在「{args.project}」里做：{request}。开始吗？"

    @tool(
        "code.dispatch",
        Permission.L3A,
        params=DispatchArgs,
        timeout_s=_DISPATCH_TIMEOUT_S,
        confirm=confirm,
        done_reply=DISPATCHED_REPLY,
        description=(
            "把活派给后台的编码代理去做：改代码、做 PPT、整理文件这类要在电脑上动手的事。"
            "代理在这个目录的副本里干（git 仓库新开分支，别的目录复制一份），原来的不动；做完会主动告诉用户。"
            f"能派活的目录只有这些：{names}。用户没说是哪个、或者说的不在里面，先问清楚，不要猜。"
            "用户说接着做、继续之前那个活，不要用它重新派（前面做的就白做了），用 code.followup"
        ),
    )
    async def dispatch(args: DispatchArgs, ctx: ToolContext) -> ToolResult:
        try:
            task = await agent.dispatch(args.project, args.request)
        except DispatchRefused as refused:
            raise ToolFailure(str(refused), user_action=None) from refused
        kind = "新开了分支" if task.workspace.branch is not None else "复制了一份"
        return ToolResult(
            f"开工了：在「{task.project}」{kind}，代理在后台干。中间不用用户批准；"
            f"它拿不准的会停下来问用户，做完会主动告诉用户，详情发手机。任务号 {task.id}"
        )

    return dispatch


class FollowUpArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")

    request: str = Field(title="追加的要求", min_length=1, description="接着要代理做的事")
    task_id: str | None = Field(
        default=None,
        description="任务号；不填就是最近派的那个。用户说的是更早的哪个，先用 code.status 查",
    )


class StopArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")

    task_id: str | None = Field(default=None, description="任务号；不填就是正在干的全停下")


def stop_tool(agent: CodeAgent) -> ToolFn:
    @tool(
        "code.stop",
        Permission.L1,
        params=StopArgs,
        description=(
            "让正在干活的代理停下，用户说停下、别做了、先停一下时用。已经改了的留着，"
            "之后用户说接着做，用 code.followup 续上"
        ),
    )
    async def stop(args: StopArgs, ctx: ToolContext) -> ToolResult:
        stopped = await agent.stop_running(args.task_id)
        if not stopped:
            return ToolResult("现在没有在干的活，没什么可停的")
        which = "、".join(f"「{task.project}」的任务 {task.id}" for task in stopped)
        return ToolResult(
            f"停下了：{which}。已经改了的留在工作目录里，用户说接着做就用 code.followup 续上"
        )

    return stop


def follow_up_tool(agent: CodeAgent) -> ToolFn:
    @tool(
        "code.followup",
        Permission.L2,
        params=FollowUpArgs,
        description=(
            '给派出去的活追加要求，代理接着原来的会话做，如"让它顺便把单测补上"。'
            '没做完就停下的（到了上限、出错、被叫停、主机重启断了）也用它接着做，request 写"接着做"或用户新说的要求。'
            "代理停下来问用户时，用户的回答也用它交回去（request 写用户的回答，照原话）。"
            "要在代理停下以后才能用；还在干时告诉用户等它做完"
        ),
    )
    async def follow_up(args: FollowUpArgs, ctx: ToolContext) -> ToolResult:
        try:
            task = await agent.follow_up(args.task_id, args.request)
        except DispatchRefused as refused:
            raise ToolFailure(str(refused), user_action=None) from refused
        return ToolResult(
            f"交给代理了，它接着在「{task.project}」里做，做完会主动告诉用户。任务号 {task.id}",
            undo_args={"task_id": task.id},
        )

    return follow_up


class StatusArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")


_STATUS_LIMIT = 3


def status_tool(agent: CodeAgent) -> ToolFn:
    @tool(
        "code.status",
        Permission.L1,
        params=StatusArgs,
        description=(
            "查派出去的活做到哪了：最近派的几个，在干的（干了多久、做了几步、它列的事做完几件）、"
            "做完了的（改了哪些文件、做好的放在哪个目录、测试过没过）、没做完停下的（为什么停、能不能接着做）、"
            "写回去了没有、写回到了哪；用户问做好的东西在哪、放哪了也用它查。"
            "代理停下来问用户时，它问了什么也在这里"
        ),
    )
    async def status(args: StatusArgs, ctx: ToolContext) -> ToolResult:
        tasks = await agent.recent(_STATUS_LIMIT)
        if not tasks:
            return ToolResult("还没派过活")
        return ToolResult(
            "\n".join(_describe(task, agent.live_progress(task.id)) for task in tasks)
        )

    return status


def _describe_running(live: LiveProgress | None) -> str:
    if live is None:
        return "代理还在干"
    minutes = int(live.running_for.total_seconds() // 60)
    said = f"代理还在干：这一次干了 {minutes} 分钟，做了 {live.steps} 步"
    if live.todos_total:
        said += f"，它列的 {live.todos_total} 件事做完了 {live.todos_done} 件"
        if live.doing:
            said += f"，正在做：{live.doing}"
    elif live.last_step_kind is not None:
        said += f"，刚才在{live.last_step_kind}"
    return said


def _describe(task: CodeTask, live: LiveProgress | None) -> str:
    head = f"- 任务号 {task.id}，「{task.project}」：{task.request}。"
    match task.status:
        case TaskStatus.RUNNING:
            return head + _describe_running(live)
        case TaskStatus.REMOVED:
            published = f"，清掉以前写回去了：{task.published_to}" if task.published_to else ""
            return head + "工作目录已经清掉了" + published
        case _:
            pass
    if task.question is not None:
        return (
            head + f"代理停下来等用户回话，它问：{task.question}\n"
            "用户回答了就用 code.followup 把回答交给它，什么时候回都行"
        )
    report = task.report
    parts = []
    if report is not None:
        parts.append("做完了" if report.finished else "没做完就停了")
        parts.append(f"测试{report.tests}" if report.tests else "没说测试结果")
    if task.changed_files is not None:
        parts.append(f"改了 {task.changed_files} 个文件")
    if task.status is TaskStatus.PUBLISHED:
        parts.append(f"已经写回去了：{task.published_to}" if task.published_to else "已经写回去了")
    elif task.status is TaskStatus.FAILED:
        parts.insert(0, "没干成")
    where = task.where_it_is()
    where_line = f"{where}。" if where else ""
    summary = f"代理说：{report.summary}" if report is not None else ""
    can_continue = ""
    if report is not None and not report.finished and task.status is not TaskStatus.PUBLISHED:
        can_continue = (
            "\n用户说接着做就用 code.followup 续上"
            if task.agent_session_id is not None
            else "\n代理没起来过，接不上，要做得重新派"
        )
    return head + "，".join(parts) + "。" + where_line + summary + can_continue


class PublishArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")

    project: str = Field(title="目录", description="这个任务派到的目录名，卡片上给用户看")
    task_id: str = Field(title="任务号", description="要发布的任务号，不知道就先用 code.status 查")


def publish_tool(agent: CodeAgent) -> ToolFn:
    def confirm(args: PublishArgs) -> str:
        return f"把派到「{args.project}」的活写回去"

    async def what_was_asked(args: PublishArgs) -> tuple[str, ...]:
        task = await agent.task(args.task_id)
        if task is None or task.project != args.project:
            raise ToolFailure("任务号和目录对不上，用 code.status 查一下", user_action=None)
        return (f"任务：{task.request}",)

    @tool(
        "code.publish",
        Permission.L3B,
        params=PublishArgs,
        timeout_s=_DISPATCH_TIMEOUT_S,
        confirm=confirm,
        extra_details=what_was_asked,
        done_reply=PUBLISHED_REPLY,
        description=(
            "把派出去的活做好的写回去：git 仓库是把任务分支推到远端（不合并），别的目录是把改过、新加的文件复制回原处。"
            "要用户在手机上批准，卡片 2 分钟内有效，过了让用户再说一次。用户说发布、推上去、放回去时用"
        ),
    )
    async def publish(args: PublishArgs, ctx: ToolContext) -> ToolResult:
        task = await agent.task(args.task_id)
        if task is None or task.project != args.project:
            raise ToolFailure("任务号和目录对不上，用 code.status 查一下", user_action=None)
        try:
            _, said = await agent.publish(args.task_id)
        except DispatchRefused as refused:
            raise ToolFailure(str(refused), user_action=None) from refused
        return ToolResult(f"「{task.project}」：{said}", fields={"where": said})

    return publish


class DiscardArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")

    project: str = Field(title="目录", description="这个任务派到的目录名，确认时念给用户听")
    task_id: str = Field(title="任务号", description="要删的任务号，不知道就先用 code.status 查")


def discard_tool(agent: CodeAgent) -> ToolFn:
    def confirm(args: DiscardArgs) -> str:
        return (
            f"把派到「{args.project}」的活的工作目录删掉，没写回去的改动就没了。要删就说确认删除。"
        )

    @tool(
        "code.discard",
        Permission.L3A,
        params=DiscardArgs,
        timeout_s=_DISPATCH_TIMEOUT_S,
        confirm=confirm,
        description=(
            "删掉派出去的活的工作目录（任务记录留着）。用户说不要了、删掉、清掉时用。"
            "不删的话跑完 7 天后也会自动清掉"
        ),
    )
    async def discard(args: DiscardArgs, ctx: ToolContext) -> ToolResult:
        task = await agent.task(args.task_id)
        if task is None or task.project != args.project:
            raise ToolFailure("任务号和目录对不上，用 code.status 查一下", user_action=None)
        try:
            await agent.discard(args.task_id)
        except DispatchRefused as refused:
            raise ToolFailure(str(refused), user_action=None) from refused
        return ToolResult(f"「{task.project}」那个任务的工作目录删掉了")

    return discard
