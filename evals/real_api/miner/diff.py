"""Field-level diff between two flattened schemas."""

from __future__ import annotations

from dataclasses import dataclass, field

from evals.real_api.cases import FieldType


@dataclass
class SchemaDiff:
    removed: dict[str, FieldType] = field(default_factory=dict)
    added: dict[str, FieldType] = field(default_factory=dict)
    # path -> (old type, new type)
    retyped: dict[str, tuple[FieldType, FieldType]] = field(default_factory=dict)


def diff_schema(old: dict[str, FieldType], new: dict[str, FieldType]) -> SchemaDiff:
    return SchemaDiff(
        removed={p: t for p, t in old.items() if p not in new},
        added={p: t for p, t in new.items() if p not in old},
        retyped={p: (t, new[p]) for p, t in old.items() if p in new and new[p] != t},
    )
