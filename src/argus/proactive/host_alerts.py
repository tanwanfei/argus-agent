from datetime import timedelta

from pydantic import BaseModel, ConfigDict

from argus.kernel.events import (
    AppSignatureExpiring,
    BackupFailed,
    BudgetThresholdReached,
    DiskSpaceLow,
    Event,
    HomeAssistantUnreachable,
)
from argus.proactive.triggers import Candidate, Trigger, trigger

_TTL = timedelta(hours=1)
_DISK_LABELS = {"data": "数据目录", "backup": "备份目录"}


class _NoParams(BaseModel):
    model_config = ConfigDict(extra="forbid")


def _alert(
    trigger_id: str, title: str, body: str, *, value: int, urgency: int, dedupe_key: str
) -> Candidate:
    return Candidate(
        trigger_id=trigger_id,
        text=f"{title}。{body}",
        value=value,
        urgency=urgency,
        ttl=_TTL,
        dedupe_key=dedupe_key,
    )


def _today(event: Event) -> str:
    return event.ts.date().isoformat()


@trigger(on=DiskSpaceLow, id="disk_space_low", params=_NoParams)
def disk_space_low(event: DiskSpaceLow, params: _NoParams) -> Candidate:
    label = _DISK_LABELS.get(event.disk_name, event.disk_name)
    return _alert(
        "disk_space_low",
        "磁盘空间不足",
        f"{label} {event.path} 所在盘只剩 {event.free_gb} GB，低于 {event.threshold_gb:g} GB",
        value=6,
        urgency=4,
        dedupe_key=f"{event.disk_name}:{_today(event)}",
    )


@trigger(on=BackupFailed, id="backup_failed", params=_NoParams)
def backup_failed(event: BackupFailed, params: _NoParams) -> Candidate:
    return _alert(
        "backup_failed",
        "库没备份成功",
        f"一小时后自动再试。原因：{event.reason}",
        value=6,
        urgency=4,
        dedupe_key=_today(event),
    )


@trigger(on=HomeAssistantUnreachable, id="home_assistant_unreachable", params=_NoParams)
def home_assistant_unreachable(event: HomeAssistantUnreachable, params: _NoParams) -> Candidate:
    since = f"{event.since.month}月{event.since.day}日 {event.since:%H:%M}"
    action = "" if event.user_action is None else f"{event.user_action}。"
    return _alert(
        "home_assistant_unreachable",
        "家里的 Home Assistant 连不上",
        f"{since}起：{event.reason}。{action}修好之前，空调和温湿度计都用不了",
        value=6,
        urgency=5,
        dedupe_key=_today(event),
    )


@trigger(on=AppSignatureExpiring, id="app_signature_expiring", params=_NoParams)
def app_signature_expiring(event: AppSignatureExpiring, params: _NoParams) -> Candidate:
    expires_at = event.expires_at.astimezone(event.ts.tzinfo)
    shown_expires_at = f"{expires_at.month}月{expires_at.day}日 {expires_at:%H:%M}"
    return _alert(
        "app_signature_expiring",
        "App 签名快到期了",
        f"阿格斯的签名 {shown_expires_at} 到期（上次连上时报的）。连着家里的 Wi-Fi 在 AltStore 里刷新一下；"
        "已经续过的话，打开阿格斯连一次就不再提醒",
        value=9,
        urgency=5,
        dedupe_key=_today(event),
    )


@trigger(on=BudgetThresholdReached, id="budget_threshold_reached", params=_NoParams)
def budget_threshold_reached(event: BudgetThresholdReached, params: _NoParams) -> Candidate:
    percent = round(event.ratio * 100)
    spent = f"本月已花 ¥{event.spent_yuan:.2f}，上限 ¥{event.budget_yuan:g}。"
    if event.ratio >= 1:
        title = "云端花费到了本月上限"
        consequence = "这个月不再调云端模型，下个月 1 日自动恢复"
        value, urgency = 8, 5
    else:
        title = f"云端花费到了本月上限的 {percent}%"
        consequence = "花到上限这个月就不再调云端模型，下个月 1 日自动恢复"
        value, urgency = 6, 4
    return _alert(
        "budget_threshold_reached",
        title,
        f"{spent}{consequence}；急用就用命令行 budget add 追加",
        value=value,
        urgency=urgency,
        dedupe_key=f"{event.ts:%Y-%m}:{percent}",
    )


HOST_ALERT_TRIGGERS: tuple[Trigger, ...] = (
    disk_space_low,
    backup_failed,
    home_assistant_unreachable,
    app_signature_expiring,
    budget_threshold_reached,
)
