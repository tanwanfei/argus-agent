import logging
from collections.abc import Awaitable, Callable

from starlette.requests import Request
from starlette.responses import PlainTextResponse, Response

from argus.codeagent.hooks import ClaudeCodeHooks, HookRejected

logger = logging.getLogger(__name__)


def claude_code_hook_route(hooks: ClaudeCodeHooks) -> Callable[[Request], Awaitable[Response]]:
    async def receive(request: Request) -> Response:
        if "origin" in request.headers:
            return _refuse(403, "浏览器页面不能调这个接口")
        media_type = request.headers.get("content-type", "").split(";")[0].strip().lower()
        if media_type != "application/json":
            return _refuse(415, "请求头 Content-Type 要是 application/json")
        try:
            payload = await request.json()
        except ValueError:
            return _refuse(400, "请求体不是合法的 UTF-8 JSON")
        if not isinstance(payload, dict):
            return _refuse(400, "请求体要是一个 JSON 对象")
        try:
            hooks.on_hook(payload)
        except HookRejected as error:
            return _refuse(400, str(error))
        return Response(status_code=204)

    return receive


def _refuse(status_code: int, reason: str) -> Response:
    logger.warning("Claude Code 的 hook 被拒（%d）：%s", status_code, reason)
    return PlainTextResponse(reason, status_code=status_code)
