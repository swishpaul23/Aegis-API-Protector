"""Compare one record against a Pydantic model and describe the drift."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ValidationError

from aegis_healer.paths import flatten, get_path, model_paths
from aegis_healer.types import DriftReport, TypeMismatch


def _loc(err: dict[str, Any]) -> str:
    return ".".join(str(p) for p in err["loc"])


def detect_drift(record: dict[str, Any], model: type[BaseModel]) -> DriftReport:
    expected = model_paths(model)
    present = flatten(record)

    missing: list[str] = []
    mismatches: list[TypeMismatch] = []
    try:
        model.model_validate(record)
    except ValidationError as exc:
        for err in exc.errors():
            path = _loc(err)
            if err["type"] == "extra_forbidden":
                continue
            if err["type"] == "missing":
                leaves = [p for p in expected if p == path or p.startswith(f"{path}.")]
                missing.extend(p for p in leaves if p not in present)
                continue
            try:
                received = type(get_path(record, path)).__name__
            except KeyError:
                received = "missing"
            # A nested object replaced by a scalar counts as all its leaves missing.
            leaves = [p for p in expected if p.startswith(f"{path}.")]
            if leaves:
                missing.extend(leaves)
                continue
            mismatches.append(
                TypeMismatch(path=path, expected=expected.get(path, "?"), received=received)
            )

    unexpected = [
        p for p in present if p not in expected and not any(e.startswith(f"{p}.") for e in expected)
    ]
    return DriftReport(
        missing=sorted(set(missing)),
        unexpected=sorted(unexpected),
        type_mismatches=mismatches,
    )
