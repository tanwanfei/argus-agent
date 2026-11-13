from dataclasses import dataclass
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator

from argus.infra.integrations.homeassistant.client import EntityState, HomeAssistantClient
from argus.kernel.spoken_number import parse_chinese_number
from argus.kernel.tools import Permission, ToolContext, ToolFailure, ToolFn, ToolResult, tool

_TIMEOUT_S = 15
_READ_TIMEOUT_S = 8
_OFF = "off"
_UNAVAILABLE = "unavailable"
_UNKNOWN = "unknown"


class NoArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")


class SetTemperatureArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")

    temperature: int = Field(description="目标温度，摄氏度整数，如 26")

    @field_validator("temperature", mode="before")
    @classmethod
    def _accept_chinese_numbers(cls, value: object) -> object:
        if isinstance(value, str):
            chinese_number = parse_chinese_number(value)
            if chinese_number is not None:
                return chinese_number
        return value


class RestoreAirConditionerArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")

    hvac_mode: str | None
    temperature: float | None


@dataclass(frozen=True)
class AirConditionerTools:
    turn_on: ToolFn
    turn_off: ToolFn
    set_temperature: ToolFn
    restore: ToolFn


def room_climate_tool(
    client: HomeAssistantClient, *, temperature_entity: str | None, humidity_entity: str | None
) -> ToolFn:
    @tool(
        "ha.room_climate",
        Permission.L1,
        params=NoArgs,
        timeout_s=_READ_TIMEOUT_S,
        personal=False,
        description="查家里温湿度计现在的读数：室内温度和湿度。用户问家里几度、湿不湿时用",
    )
    async def room_climate(args: NoArgs, ctx: ToolContext) -> ToolResult:
        spoken_parts: list[str] = []
        if temperature_entity is not None:
            temperature = await _read_number(client, temperature_entity, unit="°C")
            if temperature is not None:
                spoken_parts.append(f"{round(temperature, 1):g} 度")
        if humidity_entity is not None:
            humidity = await _read_number(client, humidity_entity, unit="%")
            if humidity is not None:
                spoken_parts.append(f"湿度 {round(humidity):d}%")
        if not spoken_parts:
            raise ToolFailure(
                "温湿度计现在读不到",
                user_action="在米家 App 里看看温湿度计在不在线、电量够不够",
            )
        spoken = "，".join(spoken_parts)
        return ToolResult(f"家里现在{spoken}", fields={"reading": spoken})

    return room_climate


def air_conditioner_tools(client: HomeAssistantClient, entity_id: str) -> AirConditionerTools:
    @tool(
        "ha.ac_on",
        Permission.L2,
        params=NoArgs,
        timeout_s=_TIMEOUT_S,
        personal=False,
        description="打开家里的空调，用它上次的模式和温度。用户说开空调时用",
    )
    async def turn_on(args: NoArgs, ctx: ToolContext) -> ToolResult:
        before = await _read_state(client, entity_id)
        await client.call_service("climate", "turn_on", {"entity_id": entity_id})
        undo_args = _undo_args(mode=_OFF) if before.state == _OFF else None
        return ToolResult("空调开了", undo_args=undo_args)

    @tool(
        "ha.ac_off",
        Permission.L2,
        params=NoArgs,
        timeout_s=_TIMEOUT_S,
        personal=False,
        description="关掉家里的空调。用户说关空调时用",
    )
    async def turn_off(args: NoArgs, ctx: ToolContext) -> ToolResult:
        before = await _read_state(client, entity_id)
        await client.call_service("climate", "turn_off", {"entity_id": entity_id})
        was_running = before.state not in (_OFF, _UNKNOWN)
        return ToolResult(
            "空调关了", undo_args=_undo_args(mode=before.state) if was_running else None
        )

    @tool(
        "ha.ac_set_temperature",
        Permission.L2,
        params=SetTemperatureArgs,
        timeout_s=_TIMEOUT_S,
        personal=False,
        description=(
            "把家里空调的目标温度调到几度（摄氏度整数）。空调关着时会先打开。"
            "只能设一个具体的温度，调不了「调高一点」「调低两度」这类相对的说法"
        ),
    )
    async def set_temperature(args: SetTemperatureArgs, ctx: ToolContext) -> ToolResult:
        before = await _read_state(client, entity_id)
        _check_in_range(args.temperature, before)
        turned_on = before.state == _OFF
        if turned_on:
            await client.call_service("climate", "turn_on", {"entity_id": entity_id})
        await client.call_service(
            "climate", "set_temperature", {"entity_id": entity_id, "temperature": args.temperature}
        )
        previous = before.attributes.get("temperature")
        text = f"空调调到 {args.temperature} 度" + (
            "（原来是关着的，已经先打开了）" if turned_on else ""
        )
        return ToolResult(
            text,
            fields={"temperature": str(args.temperature)},
            undo_args=_undo_args(
                mode=_OFF if turned_on else None,
                temperature=previous if previous != args.temperature else None,
            ),
        )

    @tool(
        "ha.ac_restore",
        Permission.L2,
        params=RestoreAirConditionerArgs,
        timeout_s=_TIMEOUT_S,
        personal=False,
        description="把空调换回操作之前的样子（撤销用，不给模型）",
    )
    async def restore(args: RestoreAirConditionerArgs, ctx: ToolContext) -> ToolResult:
        if args.temperature is not None:
            await client.call_service(
                "climate",
                "set_temperature",
                {"entity_id": entity_id, "temperature": args.temperature},
            )
        if args.hvac_mode == _OFF:
            await client.call_service("climate", "turn_off", {"entity_id": entity_id})
        elif args.hvac_mode is not None:
            await client.call_service(
                "climate", "set_hvac_mode", {"entity_id": entity_id, "hvac_mode": args.hvac_mode}
            )
        return ToolResult("空调换回原来的了")

    return AirConditionerTools(turn_on, turn_off, set_temperature, restore)


async def _read_state(client: HomeAssistantClient, entity_id: str) -> EntityState:
    state = await client.state(entity_id)
    if state.state == _UNAVAILABLE:
        raise ToolFailure(
            "空调现在连不上（HA 里显示设备离线）",
            user_action="在米家 App 里看看空调伴侣在不在线、插头有没有松",
        )
    return state


async def _read_number(client: HomeAssistantClient, entity_id: str, *, unit: str) -> float | None:
    state = await client.state(entity_id)
    if state.state in (_UNAVAILABLE, _UNKNOWN):
        return None
    if state.attributes.get("unit_of_measurement") != unit:
        raise ToolFailure(
            f"{entity_id} 报的单位不是 {unit}",
            user_action="检查 HA 的单位设置，或 argus.yaml 里写的是不是这个传感器",
        )
    try:
        return float(state.state)
    except ValueError as error:
        raise ToolFailure(
            f"{entity_id} 的读数 {state.state} 不是数字",
            user_action="检查 argus.yaml 里写的是不是温度、湿度的传感器",
        ) from error


def _check_in_range(temperature: int, state: EntityState) -> None:
    lowest = state.attributes.get("min_temp")
    highest = state.attributes.get("max_temp")
    if lowest is None or highest is None:
        return
    if not lowest <= temperature <= highest:
        raise ToolFailure(f"这台空调只能调 {lowest:g} 到 {highest:g} 度", user_action=None)


def _undo_args(*, mode: str | None, temperature: Any = None) -> dict[str, Any] | None:
    if mode is None and temperature is None:
        return None
    return {"hvac_mode": mode, "temperature": temperature}
