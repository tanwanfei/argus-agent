import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, ValidationError

from argus.llm.gateway import LlmRequest, Message, ModelGateway, Purpose, Role, Tier

_REPLY_MAX_CHARS = 300

_EXTRACT_MAX_TOKENS = 600

_WEEKDAYS = ("周一", "周二", "周三", "周四", "周五", "周六", "周日")

_DEFAULT_CONFIDENCE = 0.7

_SYSTEM_PROMPT = """你是记忆整理员。下面是用户和管家的一轮对话，有时前面还附着同一段对话里的前几轮。你只从【这一轮用户说的话】里整理值得长期记住的内容；管家的回复只用来帮你理解上下文，里面的内容不算用户说的。

只记这三种，其余一律不记：
1. explicit：用户明确要求记住的事。
2. stable_fact：稳定的事实——人名、关系、偏好、过敏、作息、重要日期、约束。写成"主体、属性、值"：主体说用户自己时写"我"，说别人写称呼（妈妈、豆豆）；属性尽量短（过敏、咖啡、生日）。
3. event：事件本身有后续价值——做了什么决定、发生了什么事、定了什么安排；用户特意讲给你听的往事、和身边人之间发生过的事也算。写成一句话摘要，并给出发生的日期 when（YYYY-MM-DD，按下面给的对话时间推算"明天""下周三"；推不出来就不写 when）。

不记的：闲聊、问天气问新闻、让管家做事的指令（开灯、定闹钟、查东西，让管家把"这里"存成某个地点）、情绪发泄、一次性的日常小事（今天地铁挤、中午吃了面）、管家自己说的话。
用户只是在问的（问自己过去的事、问某个人是谁、怎么样），什么都不记：答案是管家查来念给用户听的，不是用户这一轮告诉你的。问句里提到的名字、称呼也不记，语音识别可能听错了。
管家回复写成"（这一轮管家调了工具……这里略去）"的，回复是工具查到或办完的结果，不是用户说的：只看用户这句话本身有没有新告诉你的事。
不要记密码、验证码、完整的证件号和银行卡号。不要把原话整段抄进来。
如果这句话是在更正或替换以前说过的（"改成""不再""换成""搬到"），replaces 写 true；只是再补充另一个值（又对另一样东西过敏）写 false。
用户消息开头可能有"已经记着的内容"，每行是"主体的属性：值"：说的是同一件事时，主体和属性照它的写法写，不要另起名字；用户是在改它，replaces 写 true；已经记着、没有变化的不要再输出。
附着的前几轮只用来帮你理解这一轮，它们每一轮都已经单独整理过了：一件事分几句才说完、这一轮要和前几轮合起来才说清的，按合起来的写成一条；前几轮里自己就说清了的事已经记过了，不要再输出——这一轮只是在问和它有关的问题、让管家帮忙、道谢，或者把它换个说法再说一遍，都输出空的。
confidence 是你有多确定，0 到 1。

只输出一个 JSON，不要加任何解释：
{"facts":[{"subject":"","attribute":"","value":"","reason":"explicit 或 stable_fact","replaces":false,"confidence":0.8}],"episodes":[{"summary":"","when":"YYYY-MM-DD","reason":"event"}]}
没有值得记的就输出：{"facts":[],"episodes":[]}

例子（对话发生在 2026-03-02 周一）：
用户说：我对花生过敏
输出：{"facts":[{"subject":"我","attribute":"过敏","value":"花生","reason":"stable_fact","replaces":false,"confidence":0.9}],"episodes":[]}
用户说：今天天气怎么样
输出：{"facts":[],"episodes":[]}
用户说：下周三我要去北京出差
输出：{"facts":[],"episodes":[{"summary":"下周三去北京出差","when":"2026-03-11","reason":"event"}]}
用户说：我现在改喝拿铁了
输出：{"facts":[{"subject":"我","attribute":"咖啡","value":"拿铁","reason":"stable_fact","replaces":true,"confidence":0.85}],"episodes":[]}
用户说：把客厅灯关了
输出：{"facts":[],"episodes":[]}
用户说：我小时候干过什么蠢事？
输出：{"facts":[],"episodes":[]}
用户说：把这里记成我的家
输出：{"facts":[],"episodes":[]}
用户说：备用钥匙放在门口鞋柜的抽屉里
输出：{"facts":[{"subject":"我","attribute":"备用钥匙","value":"门口鞋柜的抽屉里","reason":"stable_fact","replaces":false,"confidence":0.85}],"episodes":[]}

例子（附着前几轮）：
前几轮：用户说"我第一份工作是在一家小书店"；用户说"后来书店倒闭了"
这一轮用户说：我就是那时候决定转行写代码的
输出：{"facts":[],"episodes":[{"summary":"第一份工作在小书店，书店倒闭后决定转行写代码","reason":"event"}]}
前几轮：用户说"下周三我要去北京出差"
这一轮用户说：帮我看看那几天北京的天气
输出：{"facts":[],"episodes":[]}"""


class ExtractionFormatError(Exception):
    pass


@dataclass(frozen=True)
class PriorTurn:
    utterance: str
    reply: str


@dataclass(frozen=True)
class ConversationTurn:
    happened_at: datetime
    utterance: str
    reply: str
    trace_id: str | None
    context: tuple[PriorTurn, ...] = ()

    @classmethod
    def from_payload(cls, payload: Mapping[str, Any]) -> "ConversationTurn":
        try:
            happened_at = datetime.fromisoformat(str(payload["ts"]))
            utterance = str(payload["utterance"])
            reply = str(payload["reply"])
            context = tuple(
                PriorTurn(utterance=str(prior["utterance"]), reply=str(prior["reply"]))
                for prior in payload.get("context", ())
            )
        except (KeyError, TypeError, ValueError) as error:
            raise ValueError("抽取任务的内容不全或时刻看不懂") from error
        if happened_at.tzinfo is None:
            raise ValueError("抽取任务里的时刻没带时区")
        trace_id = payload.get("trace_id")
        return cls(
            happened_at=happened_at,
            utterance=utterance,
            reply=reply,
            trace_id=None if trace_id is None else str(trace_id),
            context=context,
        )


@dataclass(frozen=True)
class ExtractedFact:
    subject: str
    attribute: str
    value: str
    reason: str | None
    replaces: bool
    confidence: float


@dataclass(frozen=True)
class ExtractedEpisode:
    summary: str
    reason: str | None
    when: date | None


@dataclass(frozen=True)
class Extracted:
    facts: tuple[ExtractedFact, ...] = ()
    episodes: tuple[ExtractedEpisode, ...] = ()


class _FactItem(BaseModel):
    model_config = ConfigDict(extra="ignore", coerce_numbers_to_str=True)

    subject: str
    attribute: str
    value: str
    reason: str | None = None
    replaces: bool = False
    confidence: float = _DEFAULT_CONFIDENCE


class _EpisodeItem(BaseModel):
    model_config = ConfigDict(extra="ignore", coerce_numbers_to_str=True)

    summary: str
    reason: str | None = None
    when: str | None = None


def reply_for_extraction(reply: str, tools_called: Sequence[str]) -> str:
    if not tools_called:
        return reply
    tool_names = "、".join(dict.fromkeys(tools_called))
    return f"（这一轮管家调了工具（{tool_names}），回复是工具查到或办完的结果，不是用户说的，这里略去）"


def build_extraction_request(turn: ConversationTurn, known: Sequence[str] = ()) -> LlmRequest:
    happened_at = turn.happened_at
    known_lines = "".join(f"- {line}\n" for line in known)
    known_text = f"已经记着的内容（对照用，没变化的不要再输出）：\n{known_lines}" if known else ""
    context_lines = "".join(
        f"用户说：{prior.utterance}\n管家回复：{_shorten(prior.reply)}\n" for prior in turn.context
    )
    context_text = (
        f"前几轮（只帮你理解这一轮，里面自己说清了的事已经记过）：\n{context_lines}这一轮：\n"
        if turn.context
        else ""
    )
    user_text = (
        f"{known_text}{context_text}对话发生的时间：{happened_at:%Y-%m-%d} "
        f"{_WEEKDAYS[happened_at.weekday()]} {happened_at:%H:%M}\n"
        f"用户说：{turn.utterance}\n管家回复：{_shorten(turn.reply)}"
    )
    return LlmRequest(
        tier=Tier.MAIN,
        purpose=Purpose.EXTRACT,
        messages=(Message(Role.SYSTEM, _SYSTEM_PROMPT), Message(Role.USER, user_text)),
        max_tokens=_EXTRACT_MAX_TOKENS,
    )


def _shorten(reply: str) -> str:
    return reply if len(reply) <= _REPLY_MAX_CHARS else reply[:_REPLY_MAX_CHARS] + "……"


def parse_extraction(text: str) -> Extracted:
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end <= start:
        raise ExtractionFormatError("抽取结果里没有 JSON")
    try:
        parsed = json.loads(text[start : end + 1])
    except json.JSONDecodeError as error:
        raise ExtractionFormatError("抽取结果的 JSON 解析不了") from error
    if not isinstance(parsed, dict):
        raise ExtractionFormatError("抽取结果的 JSON 不是对象")
    return Extracted(
        facts=tuple(_parse_facts(parsed.get("facts"))),
        episodes=tuple(_parse_episodes(parsed.get("episodes"))),
    )


def _parse_facts(raw_items: object) -> list[ExtractedFact]:
    facts = []
    for raw_item in raw_items if isinstance(raw_items, list) else []:
        try:
            item = _FactItem.model_validate(raw_item)
        except ValidationError:
            continue
        facts.append(
            ExtractedFact(
                subject=item.subject,
                attribute=item.attribute,
                value=item.value,
                reason=item.reason,
                replaces=item.replaces,
                confidence=min(max(item.confidence, 0.0), 1.0),
            )
        )
    return facts


def _parse_episodes(raw_items: object) -> list[ExtractedEpisode]:
    episodes = []
    for raw_item in raw_items if isinstance(raw_items, list) else []:
        try:
            item = _EpisodeItem.model_validate(raw_item)
        except ValidationError:
            continue
        episodes.append(
            ExtractedEpisode(summary=item.summary, reason=item.reason, when=_parse_date(item.when))
        )
    return episodes


def _parse_date(text: str | None) -> date | None:
    if text is None:
        return None
    try:
        return date.fromisoformat(text.strip())
    except ValueError:
        return None


class Extractor:
    def __init__(self, gateway: ModelGateway) -> None:
        self._gateway = gateway

    async def extract(self, turn: ConversationTurn, known: Sequence[str] = ()) -> Extracted:
        response = await self._gateway.complete(build_extraction_request(turn, known))
        return parse_extraction(response.text)
