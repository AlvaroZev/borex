from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any


class ParamType(str, Enum):
    INT = "int"
    FLOAT = "float"
    BOOL = "bool"
    CHOICE = "choice"


@dataclass(frozen=True)
class ParamDef:
    """Typed parameter schema for strategies and future AI optimizers."""

    name: str
    type: ParamType
    default: Any
    min: float | int | None = None
    max: float | int | None = None
    step: float | int | None = None
    choices: tuple[Any, ...] | None = None
    description: str = ""

    def clamp(self, value: Any) -> Any:
        if self.type == ParamType.BOOL:
            return bool(value)
        if self.type == ParamType.CHOICE:
            if value not in (self.choices or ()):
                return self.default
            return value
        if self.type == ParamType.INT:
            v = int(round(float(value)))
            if self.min is not None:
                v = max(int(self.min), v)
            if self.max is not None:
                v = min(int(self.max), v)
            return v
        if self.type == ParamType.FLOAT:
            v = float(value)
            if self.min is not None:
                v = max(float(self.min), v)
            if self.max is not None:
                v = min(float(self.max), v)
            return v
        return value

    def grid_values(self) -> list[Any]:
        if self.type == ParamType.BOOL:
            return [False, True]
        if self.type == ParamType.CHOICE:
            return list(self.choices or [self.default])
        if self.min is None or self.max is None:
            return [self.default]
        step = self.step or (1 if self.type == ParamType.INT else (self.max - self.min) / 10)
        vals: list[Any] = []
        cur = float(self.min)
        while cur <= float(self.max) + 1e-9:
            vals.append(self.clamp(cur))
            cur += float(step)
        return vals

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "type": self.type.value,
            "default": self.default,
            "min": self.min,
            "max": self.max,
            "step": self.step,
            "choices": list(self.choices) if self.choices else None,
            "description": self.description,
        }


def resolve_params(schema: list[ParamDef], overrides: dict | None = None) -> dict:
    out = {p.name: p.default for p in schema}
    if overrides:
        for p in schema:
            if p.name in overrides:
                out[p.name] = p.clamp(overrides[p.name])
    return out
