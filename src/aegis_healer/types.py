"""Shared data types. A fix is always one of these objects: data, never code."""

from __future__ import annotations

import math
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

Transform = Literal[
    "none",
    "to_int",
    "to_float",
    "to_str",
    "to_bool",
    "parse_datetime",
    "epoch_to_iso",
    "value_map",
    "concat",
]

# Transforms that take no parameters, in the order the matcher tries them.
SIMPLE_TRANSFORMS: tuple[Transform, ...] = (
    "none",
    "to_int",
    "to_float",
    "to_str",
    "to_bool",
    "parse_datetime",
    "epoch_to_iso",
)

Scalar = str | int | float | bool | None
Origin = Literal["deterministic", "agent"]


class FieldMapping(BaseModel):
    """Move/convert one value from the API payload to a field of the target model."""

    model_config = ConfigDict(extra="forbid")

    source: str | list[str]
    target: str
    transform: Transform = "none"
    value_map: dict[str, Scalar] | None = None
    separator: str = " "
    scale: float | None = None
    origin: Origin = "deterministic"
    confidence: float = Field(default=1.0, ge=0.0, le=1.0)
    reason: str = ""

    @model_validator(mode="after")
    def _check_shape(self) -> FieldMapping:
        if self.transform == "concat":
            if not isinstance(self.source, list) or len(self.source) < 2:
                raise ValueError("concat needs a list of at least 2 sources")
        elif not isinstance(self.source, str):
            raise ValueError("only concat may take a list of sources")
        if self.transform == "value_map" and not self.value_map:
            raise ValueError("value_map transform needs a value_map")
        if self.scale is not None and (not math.isfinite(self.scale) or self.scale == 0):
            raise ValueError("scale must be finite and non-zero")
        return self

    @property
    def sources(self) -> list[str]:
        return self.source if isinstance(self.source, list) else [self.source]


class Unmappable(BaseModel):
    """A field that cannot be filled honestly from the payload."""

    target: str
    reason: str


class MappingPatch(BaseModel):
    mappings: list[FieldMapping] = Field(default_factory=list)
    unmappable: list[Unmappable] = Field(default_factory=list)


class TypeMismatch(BaseModel):
    path: str
    expected: str
    received: str


class DriftReport(BaseModel):
    missing: list[str] = Field(default_factory=list)
    unexpected: list[str] = Field(default_factory=list)
    type_mismatches: list[TypeMismatch] = Field(default_factory=list)

    @property
    def is_clean(self) -> bool:
        return not (self.missing or self.type_mismatches)


class PatchCheck(BaseModel):
    ok: bool
    errors: list[str] = Field(default_factory=list)


class MatchResult(BaseModel):
    resolved: list[FieldMapping] = Field(default_factory=list)
    unresolved: list[str] = Field(default_factory=list)
