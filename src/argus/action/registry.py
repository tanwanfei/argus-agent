from dataclasses import replace
from enum import StrEnum

from argus.kernel.tools import Permission, ToolFn, ToolSpec

_CONFIRMED_PERMISSIONS = frozenset({Permission.L3A, Permission.L3B})


class OverrideOutcome(StrEnum):
    APPLIED = "applied"
    UNKNOWN_TOOL = "unknown_tool"
    WITHDRAWN = "withdrawn"


class ToolRegistry:
    def __init__(self) -> None:
        self._tools_by_name: dict[str, ToolFn] = {}
        self._undo_by_name: dict[str, ToolFn] = {}

    def register(self, fn: ToolFn, *, undo: ToolFn | None = None) -> None:
        name = fn.spec.name
        if name in self._tools_by_name:
            raise ValueError(f"工具 {name} 已经注册过了")
        is_l2 = fn.spec.permission is Permission.L2
        if is_l2 and undo is None:
            raise ValueError(f"工具 {name} 是 L2，注册时要带撤销工具（undo=）")
        if not is_l2 and undo is not None:
            raise ValueError(f"工具 {name} 不是 L2，不该带撤销工具")
        is_l3 = fn.spec.permission in _CONFIRMED_PERMISSIONS
        if is_l3 and fn.spec.confirm is None:
            raise ValueError(f"工具 {name} 是 L3，要带确认模板（confirm=）")
        if not is_l3 and fn.spec.confirm is not None:
            raise ValueError(f"工具 {name} 不是 L3，不该带确认模板")
        self._tools_by_name[name] = fn
        if undo is not None:
            self._undo_by_name[name] = undo

    def override(
        self, name: str, *, permission: Permission | None, personal: bool | None
    ) -> OverrideOutcome:
        fn = self._tools_by_name.get(name)
        if fn is None:
            return OverrideOutcome.UNKNOWN_TOOL
        spec = fn.spec
        new_permission = spec.permission if permission is None else permission
        is_l3 = new_permission in _CONFIRMED_PERMISSIONS
        if is_l3 and spec.confirm is None:
            del self._tools_by_name[name]
            self._undo_by_name.pop(name, None)
            return OverrideOutcome.WITHDRAWN
        new_spec = replace(
            spec,
            permission=new_permission,
            personal=spec.personal if personal is None else personal,
            confirm=spec.confirm if is_l3 else None,
        )
        self._tools_by_name[name] = replace(fn, spec=new_spec)
        if new_permission is not Permission.L2:
            self._undo_by_name.pop(name, None)
        return OverrideOutcome.APPLIED

    def get(self, name: str) -> ToolFn | None:
        return self._tools_by_name.get(name)

    def undo_of(self, name: str) -> ToolFn | None:
        return self._undo_by_name.get(name)

    def specs(self) -> tuple[ToolSpec, ...]:
        return tuple(fn.spec for fn in self._tools_by_name.values())
