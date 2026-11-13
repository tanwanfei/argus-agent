import asyncio
import json
import logging
from collections.abc import AsyncGenerator, Mapping
from contextlib import aclosing
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Protocol

from argus.kernel.clock import Clock
from argus.kernel.events import BudgetThresholdReached, EventBus
from argus.kernel.tools import ToolCall, ToolSpec
from argus.kernel.tracing import current_trace_id
from argus.llm.gateway import (
    ChatMessage,
    CloudHealth,
    LlmDelta,
    LlmError,
    LlmRequest,
    LlmResponse,
    ModelGateway,
    Purpose,
    Reasoning,
    TextDelta,
    Tier,
    ToolCallsMessage,
    Usage,
)

logger = logging.getLogger(__name__)

_BEIJING_TIME = timezone(timedelta(hours=8))

_WARN_RATIO = 0.8
_EXHAUSTED_RATIO = 1.0


@dataclass(frozen=True)
class TokenPrice:
    input_cache_hit: float
    input_cache_miss: float
    output: float

    def cost_yuan(self, usage: Usage) -> float:
        cache_miss_tokens = usage.input_tokens - usage.cached_input_tokens
        cost_per_million = (
            usage.cached_input_tokens * self.input_cache_hit
            + cache_miss_tokens * self.input_cache_miss
            + usage.output_tokens * self.output
        )
        return cost_per_million / 1_000_000


@dataclass(frozen=True)
class ModelPricing:
    regular: TokenPrice
    peak: TokenPrice | None = None
    peak_hours: tuple[tuple[int, int], ...] = ()

    def price_at(self, moment: datetime) -> TokenPrice:
        if self.peak is None:
            return self.regular
        beijing_moment = moment.astimezone(_BEIJING_TIME)
        is_weekday = beijing_moment.weekday() < 5
        in_peak_hours = any(start <= beijing_moment.hour < end for start, end in self.peak_hours)
        return self.peak if is_weekday and in_peak_hours else self.regular


@dataclass(frozen=True)
class CostEntry:
    ts: datetime
    trace_id: str | None
    purpose: Purpose
    tier: Tier
    usage: Usage
    cost_yuan: float
    estimated: bool = False


class CostLedger(Protocol):
    async def record(self, entry: CostEntry) -> None: ...

    async def spent_since(self, start: datetime) -> float: ...

    async def extra_budget_yuan(self, month: str) -> float: ...


def budget_month(moment: datetime) -> datetime:
    beijing_moment = moment.astimezone(_BEIJING_TIME)
    return beijing_moment.replace(day=1, hour=0, minute=0, second=0, microsecond=0)


@dataclass(frozen=True)
class _BudgetCheck:
    spent_before: float
    budget_yuan: float


class BudgetExhaustedError(LlmError):
    pass


class Budgeted:
    def __init__(
        self,
        inner: ModelGateway,
        *,
        ledger: CostLedger,
        pricing: Mapping[str, ModelPricing],
        estimate_model: str,
        monthly_budget_yuan: float,
        clock: Clock,
        bus: EventBus,
    ) -> None:
        if estimate_model not in pricing:
            raise ValueError(f"估账用的模型 {estimate_model} 不在单价表里")
        self._inner = inner
        self._ledger = ledger
        self._pricing = dict(pricing)
        self._estimate_model = estimate_model
        self._bus = bus
        self._monthly_budget_yuan = monthly_budget_yuan
        self._clock = clock
        self._exhausted_month: datetime | None = None

    async def complete(self, request: LlmRequest) -> LlmResponse:
        check = await self._check_budget()
        response = await self._inner.complete(request)
        cost_yuan = await self._record(request, response.usage)
        self._report_if_crossed(check, check.spent_before + cost_yuan)
        return response

    def health(self) -> CloudHealth:
        if self._exhausted_month == budget_month(self._clock.now()):
            return CloudHealth.DOWN
        return self._inner.health()

    async def probe(self) -> None:
        await self._check_budget()
        await self._inner.probe()

    async def stream(self, request: LlmRequest) -> AsyncGenerator[LlmDelta]:
        check = await self._check_budget()
        received_chars = 0
        usage: Usage | None = None
        try:
            async with aclosing(self._inner.stream(request)) as deltas:
                async for delta in deltas:
                    if isinstance(delta, Usage):
                        usage = delta
                        continue
                    if isinstance(delta, TextDelta | Reasoning):
                        received_chars += len(delta.text)
                    else:
                        received_chars += _tool_call_chars(delta)
                    yield delta
        except LlmError:
            if received_chars > 0:
                await self._record_estimate(request, received_chars, check)
            raise
        except (GeneratorExit, asyncio.CancelledError):
            await self._record_estimate(request, received_chars, check)
            raise

        if usage is None:
            await self._record_estimate(request, received_chars, check)
            raise LlmError("下一层的流正常结束了，但没有给用量，这次按字数估了一笔")
        cost_yuan = await self._record(request, usage)
        self._report_if_crossed(check, check.spent_before + cost_yuan)
        yield usage

    async def _check_budget(self) -> _BudgetCheck:
        month_start = budget_month(self._clock.now())
        spent_before = await self._ledger.spent_since(month_start)
        extra_yuan = await self._ledger.extra_budget_yuan(f"{month_start:%Y-%m}")
        budget_yuan = self._monthly_budget_yuan + extra_yuan
        if spent_before >= budget_yuan:
            self._exhausted_month = month_start
            raise BudgetExhaustedError(
                f"本月（{month_start:%Y-%m}）预算用完了：已花 ¥{spent_before:.4f}，"
                f"上限 ¥{budget_yuan:g}。下个月 1 日自动恢复；"
                f"急用就用命令行 budget add 给这个月追加上限"
            )
        self._exhausted_month = None
        return _BudgetCheck(spent_before=spent_before, budget_yuan=budget_yuan)

    def _report_if_crossed(self, check: _BudgetCheck, spent_after: float) -> None:
        for ratio in (_WARN_RATIO, _EXHAUSTED_RATIO):
            line_yuan = check.budget_yuan * ratio
            if not check.spent_before < line_yuan <= spent_after:
                continue
            logger.warning(
                "本月云端调用已花 ¥%.4f，过了上限 ¥%g 的 %d%%",
                spent_after,
                check.budget_yuan,
                round(ratio * 100),
            )
            if ratio == _EXHAUSTED_RATIO:
                self._exhausted_month = budget_month(self._clock.now())
            self._bus.publish(
                BudgetThresholdReached(
                    ts=self._clock.now(),
                    ratio=ratio,
                    spent_yuan=spent_after,
                    budget_yuan=check.budget_yuan,
                )
            )

    async def _record_estimate(
        self, request: LlmRequest, received_chars: int, check: _BudgetCheck
    ) -> None:
        input_chars = sum(_message_chars(message) for message in request.messages) + sum(
            _tool_definition_chars(spec) for spec in request.tools
        )
        usage = Usage(
            model=self._estimate_model,
            input_tokens=input_chars,
            cached_input_tokens=0,
            output_tokens=received_chars,
        )
        cost_yuan = await asyncio.shield(self._record(request, usage, estimated=True))
        self._report_if_crossed(check, check.spent_before + cost_yuan)

    async def _record(self, request: LlmRequest, usage: Usage, *, estimated: bool = False) -> float:
        pricing = self._pricing.get(usage.model)
        if pricing is None:
            raise LookupError(f"记不了账：models.yaml 里没有模型 {usage.model} 的单价")
        finished_at = self._clock.now()
        entry = CostEntry(
            ts=finished_at,
            trace_id=current_trace_id(),
            purpose=request.purpose,
            tier=request.tier,
            usage=usage,
            cost_yuan=pricing.price_at(finished_at).cost_yuan(usage),
            estimated=estimated,
        )
        await self._ledger.record(entry)
        return entry.cost_yuan


def _message_chars(message: ChatMessage) -> int:
    if isinstance(message, ToolCallsMessage):
        return (
            len(message.content)
            + len(message.reasoning)
            + sum(_tool_call_chars(call) for call in message.calls)
        )
    return len(message.content)


def _tool_call_chars(tool_call: ToolCall) -> int:
    return len(tool_call.name) + len(json.dumps(dict(tool_call.args), ensure_ascii=False))


def _tool_definition_chars(spec: ToolSpec) -> int:
    schema = json.dumps(spec.params.model_json_schema(), ensure_ascii=False)
    return len(spec.name) + len(spec.description) + len(schema)
