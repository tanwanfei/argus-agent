from argus.kernel.clock import Clock
from argus.kernel.tracing import current_trace_id
from argus.llm.budgeted import CostEntry, CostLedger, ModelPricing, budget_month
from argus.llm.gateway import Purpose, Tier, Usage


class AgentBilling:
    def __init__(
        self,
        *,
        ledger: CostLedger,
        pricing: ModelPricing,
        monthly_budget_yuan: float,
        clock: Clock,
    ) -> None:
        self._ledger = ledger
        self._pricing = pricing
        self._monthly_budget_yuan = monthly_budget_yuan
        self._clock = clock

    async def budget_left(self) -> bool:
        month_start = budget_month(self._clock.now())
        spent = await self._ledger.spent_since(month_start)
        extra = await self._ledger.extra_budget_yuan(f"{month_start:%Y-%m}")
        return spent < self._monthly_budget_yuan + extra

    async def record(self, usage: Usage) -> float:
        finished_at = self._clock.now()
        entry = CostEntry(
            ts=finished_at,
            trace_id=current_trace_id(),
            purpose=Purpose.CODEAGENT,
            tier=Tier.MAIN,
            usage=usage,
            cost_yuan=self._pricing.price_at(finished_at).cost_yuan(usage),
        )
        await self._ledger.record(entry)
        return entry.cost_yuan
