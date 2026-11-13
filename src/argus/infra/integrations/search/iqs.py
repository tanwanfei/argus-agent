import logging
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Literal

import httpx2
from pydantic import BaseModel, ConfigDict, ValidationError

from argus.kernel.tools import ToolFailure

logger = logging.getLogger(__name__)

_URL = "https://cloud-iqs.aliyuncs.com/search/unified"
_TIMEOUT = httpx2.Timeout(10.0)
_RESULTS_WANTED = 5
_LOGGED_BODY_CHARS = 200

TimeRange = Literal["day", "week", "month", "year"]
_TIME_RANGE_NAMES: dict[TimeRange, str] = {
    "day": "OneDay",
    "week": "OneWeek",
    "month": "OneMonth",
    "year": "OneYear",
}


@dataclass(frozen=True)
class SearchHit:
    title: str
    link: str
    site: str | None
    published_at: datetime | None
    snippet: str
    main_text: str | None


@dataclass(frozen=True)
class SceneHit:
    type: str
    detail: str


@dataclass(frozen=True)
class SearchResults:
    hits: list[SearchHit]
    scenes: list[SceneHit]


class IqsSearchClient:
    def __init__(self, api_key: str, *, transport: httpx2.AsyncBaseTransport | None = None) -> None:
        self._client = httpx2.AsyncClient(
            headers={"Authorization": f"Bearer {api_key}"},
            trust_env=False,
            timeout=_TIMEOUT,
            transport=transport,
        )

    async def search(self, query: str, *, time_range: TimeRange | None = None) -> SearchResults:
        body: dict[str, Any] = {
            "query": query,
            "engineType": "Generic",
            "contents": {"mainText": True, "rerankScore": True},
            "advancedParams": {"numResults": str(_RESULTS_WANTED)},
        }
        if time_range is not None:
            body["timeRange"] = _TIME_RANGE_NAMES[time_range]
        try:
            response = await self._client.post(_URL, json=body)
        except httpx2.TimeoutException as error:
            logger.warning("等搜索服务回复超时：%s", type(error).__name__)
            raise ToolFailure(
                "搜索服务没有及时回复", user_action="过一会儿再试；总是这样的话检查网络"
            ) from error
        except httpx2.RequestError as error:
            logger.warning("连不上搜索服务：%s", type(error).__name__)
            raise ToolFailure("连不上搜索服务", user_action="检查网络") from error
        if not response.is_success:
            logger.warning(
                "搜索服务返回 HTTP %d：%s", response.status_code, response.text[:_LOGGED_BODY_CHARS]
            )
            raise _failure_for(response)
        try:
            payload = _Response.model_validate_json(response.content)
        except ValidationError as error:
            logger.warning("搜索服务的回答读不懂：%s", error.error_count())
            raise ToolFailure(
                "搜索服务的回答读不懂", user_action="可能是服务改了接口，看日志里的记录"
            ) from error
        return SearchResults(
            hits=[_hit_of(item) for item in payload.pageItems],
            scenes=[SceneHit(item.type, item.detail) for item in payload.sceneItems],
        )

    async def aclose(self) -> None:
        await self._client.aclose()


def _hit_of(item: "_PageItem") -> SearchHit:
    return SearchHit(
        title=item.title,
        link=item.link,
        site=item.hostname or None,
        published_at=_parse_time(item.publishedTime),
        snippet=item.snippet,
        main_text=item.mainText or None,
    )


def _parse_time(text: str | None) -> datetime | None:
    if not text:
        return None
    try:
        moment = datetime.fromisoformat(text)
    except ValueError:
        return None
    return moment if moment.tzinfo is not None else moment.astimezone()


def _failure_for(response: httpx2.Response) -> ToolFailure:
    code = _error_code(response)
    match response.status_code, code:
        case 403, "Retrieval.Arrears":
            return ToolFailure("搜索服务欠费了", user_action="到阿里云充值，再试一次")
        case 403, "Retrieval.NotActivate":
            return ToolFailure("搜索服务还没开通", user_action="在阿里云 IQS 控制台开通通用搜索")
        case 403, "Retrieval.TestUserPeriodExpired":
            return ToolFailure(
                "搜索服务的试用期过了", user_action="在阿里云 IQS 控制台转成正式账号"
            )
        case 403, "Retrieval.NotAuthorised":
            return ToolFailure(
                "这把 API-KEY 的账号没有搜索的权限", user_action="给账号加 AliyunIQSFullAccess 权限"
            )
        case 429, "Retrieval.TestUserQueryExceeded":
            return ToolFailure(
                "搜索服务今天的试用次数用完了", user_action="明天再试，或转成正式账号"
            )
        case 429, _:
            return ToolFailure("搜索太频繁，服务限流了", user_action="过一会儿再试")
        case 404 | 401, _:
            return ToolFailure(
                "搜索服务不认 API-KEY",
                user_action="检查 .env 里的 IQS_API_KEY：可能抄错了，或在 IQS 控制台被删了",
            )
        case status, _ if status >= 500:
            return ToolFailure(f"搜索服务出错，HTTP 状态码 {status}", user_action="过一会儿再试")
        case status, _:
            return ToolFailure(
                f"搜索服务不认这个请求，HTTP 状态码 {status}",
                user_action="对着官方文档查一下接口是不是改了",
            )


def _error_code(response: httpx2.Response) -> str | None:
    try:
        body = response.json()
    except ValueError:
        return None
    if not isinstance(body, dict):
        return None
    for key in ("code", "Code", "errorCode", "ErrorCode"):
        value = body.get(key)
        if isinstance(value, str):
            return value
    return None


class _PageItem(BaseModel):
    model_config = ConfigDict(extra="ignore")

    title: str
    link: str
    snippet: str = ""
    publishedTime: str | None = None
    mainText: str | None = None
    hostname: str | None = None


class _SceneItem(BaseModel):
    model_config = ConfigDict(extra="ignore")

    type: str
    detail: str = ""


class _Response(BaseModel):
    model_config = ConfigDict(extra="ignore")

    pageItems: list[_PageItem] = []
    sceneItems: list[_SceneItem] = []
