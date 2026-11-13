import html
import sqlite3
import statistics
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone

from starlette.requests import Request
from starlette.responses import HTMLResponse

from argus.infra.sqlite.database import SqliteDatabase
from argus.infra.sqlite.llm_cost import SqliteCostLedger
from argus.kernel.clock import Clock
from argus.kernel.events import RouteKind
from argus.llm.budgeted import budget_month
from argus.proactive.feedback import USELESS_LIMIT
from argus.proactive.quota import DailyLimits

_BEIJING_TIME = timezone(timedelta(hours=8))
_RECENT_DAYS = 7
_PROACTIVE_DAYS = 14

_COST_SINCE = """
SELECT purpose, tier, COUNT(*), SUM(cost_yuan) FROM llm_cost
WHERE julianday(ts) >= julianday(:start)
GROUP BY purpose, tier ORDER BY purpose, tier
"""
_TURNS_SINCE = """
SELECT endpoint, play_start_ms, llm_first_token_ms FROM obs_turn
WHERE julianday(ts) >= julianday(:start)
"""
_ROUTES_SINCE = """
SELECT route, COUNT(*) FROM obs_turn
WHERE julianday(ts) >= julianday(:start) AND route IS NOT NULL
GROUP BY route
"""
_LOCAL_ROUTES = (RouteKind.RULE, RouteKind.CONTROL)
_ROUTE_LABELS = {
    RouteKind.RULE: "规则档",
    RouteKind.CONTROL: "控制词",
    RouteKind.CLOUD: "云端模型",
    RouteKind.DEFER: "先记下来",
}
_TOOLS_SINCE = """
SELECT tool,
       SUM(result IS 'done'), SUM(result IS 'failed'), SUM(decision IS 'deny' AND result IS NULL)
FROM gov_audit
WHERE julianday(ts) >= julianday(:start)
GROUP BY tool ORDER BY tool
"""
_SENT_SINCE = """
SELECT trigger_id, SUM(verdict = 'speak'), SUM(verdict = 'notify') FROM pro_sent
WHERE julianday(sent_at) >= julianday(:start)
GROUP BY trigger_id
"""
_USELESS_SINCE = """
SELECT trigger_id, COUNT(*) FROM pro_useless_said
WHERE julianday(said_at) >= julianday(:start)
GROUP BY trigger_id
"""
_USELESS_TOTAL = "SELECT trigger_id, useless FROM pro_trigger"
_QUOTA_SINCE = """
SELECT day, SUM(CASE verdict WHEN 'speak' THEN used ELSE 0 END),
       SUM(CASE verdict WHEN 'notify' THEN used ELSE 0 END)
FROM pro_quota WHERE day >= :start_day
GROUP BY day ORDER BY day DESC
"""


@dataclass(frozen=True)
class CostRow:
    purpose: str
    tier: str
    calls: int
    cost_yuan: float


@dataclass(frozen=True)
class LatencyRow:
    endpoint: str
    turns: int
    play_start_p50: float | None
    play_start_p90: float | None
    first_token_p50: float | None
    first_token_p90: float | None


@dataclass(frozen=True)
class ToolRow:
    tool: str
    done: int
    failed: int
    denied: int

    @property
    def success_rate(self) -> float | None:
        executed = self.done + self.failed
        return None if executed == 0 else self.done / executed


@dataclass(frozen=True)
class RouteShare:
    turns_by_route: Mapping[RouteKind, int]

    @property
    def total(self) -> int:
        return sum(self.turns_by_route.values())

    @property
    def local_rate(self) -> float | None:
        if self.total == 0:
            return None
        local = sum(self.turns_by_route.get(kind, 0) for kind in _LOCAL_ROUTES)
        return local / self.total


@dataclass(frozen=True)
class TriggerUsefulness:
    trigger_id: str
    spoken: int
    notified: int
    useless: int
    useless_total: int

    @property
    def sent(self) -> int:
        return self.spoken + self.notified

    @property
    def stopped(self) -> bool:
        return self.useless_total >= USELESS_LIMIT


@dataclass(frozen=True)
class Usefulness:
    triggers: Sequence[TriggerUsefulness]

    @property
    def sent(self) -> int:
        return sum(row.sent for row in self.triggers)

    @property
    def useless(self) -> int:
        return sum(row.useless for row in self.triggers)

    @property
    def rate(self) -> float | None:
        if self.sent == 0:
            return None
        return max(0.0, 1 - self.useless / self.sent)


@dataclass(frozen=True)
class QuotaDay:
    day: date
    spoken: int
    notified: int


@dataclass(frozen=True)
class Dashboard:
    generated_at: datetime
    cost_today: Sequence[CostRow]
    cost_this_month: Sequence[CostRow]
    budget_yuan: float
    latency: Sequence[LatencyRow]
    routes: RouteShare
    tools: Sequence[ToolRow] | None
    usefulness: Usefulness
    quota_days: Sequence[QuotaDay]
    daily_limits: DailyLimits

    @property
    def spent_this_month(self) -> float:
        return sum(row.cost_yuan for row in self.cost_this_month)


def p50_p90(values: Sequence[int]) -> tuple[float, float]:
    if len(values) == 1:
        return values[0], values[0]
    p90 = statistics.quantiles(values, n=10, method="inclusive")[8]
    return statistics.median(values), p90


async def collect(
    argus_db: SqliteDatabase,
    audit_db: SqliteDatabase | None,
    *,
    now: datetime,
    monthly_budget_yuan: float,
    daily_limits: DailyLimits,
) -> Dashboard:
    month_start = budget_month(now)
    day_start = now.astimezone(_BEIJING_TIME).replace(hour=0, minute=0, second=0, microsecond=0)
    recent_start = now - timedelta(days=_RECENT_DAYS)
    proactive_start = day_start - timedelta(days=_PROACTIVE_DAYS - 1)

    def costs_since(start: datetime) -> Callable[[sqlite3.Connection], list[CostRow]]:
        def read(connection: sqlite3.Connection) -> list[CostRow]:
            rows = connection.execute(_COST_SINCE, {"start": start.isoformat()}).fetchall()
            return [CostRow(purpose, tier, calls, cost) for purpose, tier, calls, cost in rows]

        return read

    def read_turns(connection: sqlite3.Connection) -> list[tuple[str, int | None, int | None]]:
        return connection.execute(_TURNS_SINCE, {"start": recent_start.isoformat()}).fetchall()

    def read_routes(connection: sqlite3.Connection) -> RouteShare:
        rows = connection.execute(_ROUTES_SINCE, {"start": recent_start.isoformat()}).fetchall()
        return RouteShare({RouteKind(route): turns for route, turns in rows})

    def read_tools(connection: sqlite3.Connection) -> list[ToolRow]:
        rows = connection.execute(_TOOLS_SINCE, {"start": recent_start.isoformat()}).fetchall()
        return [ToolRow(tool, done, failed, denied) for tool, done, failed, denied in rows]

    def read_usefulness(connection: sqlite3.Connection) -> Usefulness:
        since = {"start": proactive_start.isoformat()}
        sent = {
            trigger_id: (spoken, notified)
            for trigger_id, spoken, notified in connection.execute(_SENT_SINCE, since)
        }
        useless = dict(connection.execute(_USELESS_SINCE, since).fetchall())
        useless_total = dict(connection.execute(_USELESS_TOTAL).fetchall())
        trigger_ids = sorted(sent.keys() | useless.keys() | useless_total.keys())
        rows = []
        for trigger_id in trigger_ids:
            spoken, notified = sent.get(trigger_id, (0, 0))
            rows.append(
                TriggerUsefulness(
                    trigger_id,
                    spoken=spoken,
                    notified=notified,
                    useless=useless.get(trigger_id, 0),
                    useless_total=useless_total.get(trigger_id, 0),
                )
            )
        return Usefulness(rows)

    def read_quota(connection: sqlite3.Connection) -> list[QuotaDay]:
        start_day = proactive_start.date().isoformat()
        rows = connection.execute(_QUOTA_SINCE, {"start_day": start_day}).fetchall()
        return [
            QuotaDay(date.fromisoformat(day), spoken, notified) for day, spoken, notified in rows
        ]

    extra_yuan = await SqliteCostLedger(argus_db).extra_budget_yuan(f"{month_start:%Y-%m}")
    return Dashboard(
        generated_at=now,
        cost_today=await argus_db.read(costs_since(day_start)),
        cost_this_month=await argus_db.read(costs_since(month_start)),
        budget_yuan=monthly_budget_yuan + extra_yuan,
        latency=_latency_rows(await argus_db.read(read_turns)),
        routes=await argus_db.read(read_routes),
        tools=None if audit_db is None else await audit_db.read(read_tools),
        usefulness=await argus_db.read(read_usefulness),
        quota_days=await argus_db.read(read_quota),
        daily_limits=daily_limits,
    )


def _latency_rows(turns: Sequence[tuple[str, int | None, int | None]]) -> list[LatencyRow]:
    rows = []
    for endpoint in sorted({endpoint for endpoint, _, _ in turns}):
        mine = [turn for turn in turns if turn[0] == endpoint]
        play_start = [ms for _, ms, _ in mine if ms is not None]
        first_token = [ms for _, _, ms in mine if ms is not None]
        play_p50, play_p90 = p50_p90(play_start) if play_start else (None, None)
        token_p50, token_p90 = p50_p90(first_token) if first_token else (None, None)
        rows.append(LatencyRow(endpoint, len(mine), play_p50, play_p90, token_p50, token_p90))
    return rows


def render(dashboard: Dashboard) -> str:
    spent = dashboard.spent_this_month
    budget_line = (
        f"本月已花 ¥{spent:.2f}，上限 ¥{dashboard.budget_yuan:g}"
        f"（{spent / dashboard.budget_yuan:.0%}）"
    )
    sections = [
        "<h2>费用</h2>",
        f"<p>{budget_line}</p>",
        "<h3>今天</h3>",
        _cost_table(dashboard.cost_today),
        "<h3>本月</h3>",
        _cost_table(dashboard.cost_this_month),
        f"<h2>延迟（最近 {_RECENT_DAYS} 天，毫秒，从判定说完算起）</h2>",
        _latency_table(dashboard.latency),
        f"<h2>本地承接率（最近 {_RECENT_DAYS} 天的语音回合）</h2>",
        _route_share(dashboard.routes),
        f"<h2>工具（最近 {_RECENT_DAYS} 天）</h2>",
        "<p>没有审计库</p>" if dashboard.tools is None else _tool_table(dashboard.tools),
        f"<h2>主动消息（最近 {_PROACTIVE_DAYS} 天）</h2>",
        _usefulness(dashboard.usefulness),
        "<h3>当日配额</h3>",
        _quota_table(dashboard.quota_days, dashboard.daily_limits),
    ]
    generated_at = dashboard.generated_at.astimezone(_BEIJING_TIME)
    return (
        '<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width, initial-scale=1">'
        "<title>阿格斯看板</title><style>"
        "body{font-family:system-ui,sans-serif;margin:16px;max-width:860px}"
        "table{border-collapse:collapse;margin-bottom:8px}"
        "th,td{border:1px solid #ccc;padding:4px 10px;text-align:right}"
        "th:first-child,td:first-child{text-align:left}"
        "</style></head><body>"
        f"<h1>阿格斯看板</h1><p>{generated_at:%Y-%m-%d %H:%M:%S} 现算</p>"
        + "".join(sections)
        + "</body></html>"
    )


def _table(headers: Sequence[str], rows: Sequence[Sequence[str]]) -> str:
    if not rows:
        return "<p>没有记录</p>"
    head = "".join(f"<th>{html.escape(header)}</th>" for header in headers)
    body = "".join(
        "<tr>" + "".join(f"<td>{html.escape(cell)}</td>" for cell in row) + "</tr>" for row in rows
    )
    return f"<table><tr>{head}</tr>{body}</table>"


def _cost_table(rows: Sequence[CostRow]) -> str:
    return _table(
        ["用途", "档位", "调用次数", "费用（元）"],
        [[row.purpose, row.tier, str(row.calls), f"{row.cost_yuan:.4f}"] for row in rows],
    )


def _latency_table(rows: Sequence[LatencyRow]) -> str:
    return _table(
        ["端点", "回合数", "开始放音 P50", "P90", "模型首字 P50", "P90"],
        [
            [
                row.endpoint,
                str(row.turns),
                _ms(row.play_start_p50),
                _ms(row.play_start_p90),
                _ms(row.first_token_p50),
                _ms(row.first_token_p90),
            ]
            for row in rows
        ],
    )


def _route_share(share: RouteShare) -> str:
    if share.local_rate is None:
        return "<p>还没有判过路由的语音回合</p>"
    local = sum(share.turns_by_route.get(kind, 0) for kind in _LOCAL_ROUTES)
    summary = (
        f"<p>{share.local_rate:.0%}：{share.total} 个回合里 {local} 个不调模型就答了"
        "（规则档、控制词）。打字的回合不算</p>"
    )
    rows = [
        [label, str(share.turns_by_route.get(kind, 0))] for kind, label in _ROUTE_LABELS.items()
    ]
    return summary + _table(["路由", "回合数"], rows)


def _tool_table(rows: Sequence[ToolRow]) -> str:
    return _table(
        ["工具", "办成", "没办成", "被拒", "成功率"],
        [
            [
                row.tool,
                str(row.done),
                str(row.failed),
                str(row.denied),
                "—" if row.success_rate is None else f"{row.success_rate:.0%}",
            ]
            for row in rows
        ],
    )


def _usefulness(usefulness: Usefulness) -> str:
    rate = usefulness.rate
    if rate is None:
        summary = "<p>没有发给你的主动提醒。"
    else:
        spoken = sum(row.spoken for row in usefulness.triggers)
        notified = sum(row.notified for row in usefulness.triggers)
        summary = (
            f"<p>有用率 {rate:.0%}：发给你 {usefulness.sent} 条（出声 {spoken}、通知 {notified}），"
            f"说了 {usefulness.useless} 次没用。"
        )
    summary += "闹钟、你定的提醒、派活的汇报是你自己要的，不算；进简报的不算</p>"
    rows = [
        [
            row.trigger_id,
            str(row.spoken),
            str(row.notified),
            str(row.useless),
            f"{row.useless_total}（停了）" if row.stopped else str(row.useless_total),
        ]
        for row in usefulness.triggers
    ]
    return summary + _table(["触发器", "出声", "通知", "说没用", "一共说过没用"], rows)


def _quota_table(days: Sequence[QuotaDay], limits: DailyLimits) -> str:
    return _table(
        ["日期", f"出声（上限 {limits.speak_per_day}）", f"通知（上限 {limits.notify_per_day}）"],
        [[day.day.isoformat(), str(day.spoken), str(day.notified)] for day in days],
    )


def _ms(value: float | None) -> str:
    return "—" if value is None else f"{value:.0f}"


def dashboard_page(
    argus_db: SqliteDatabase,
    audit_db: SqliteDatabase | None,
    *,
    clock: Clock,
    monthly_budget_yuan: float,
    daily_limits: DailyLimits,
) -> Callable[[Request], Awaitable[HTMLResponse]]:
    async def page(request: Request) -> HTMLResponse:
        dashboard = await collect(
            argus_db,
            audit_db,
            now=clock.now(),
            monthly_budget_yuan=monthly_budget_yuan,
            daily_limits=daily_limits,
        )
        return HTMLResponse(render(dashboard))

    return page
