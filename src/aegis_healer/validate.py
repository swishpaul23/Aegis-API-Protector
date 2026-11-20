"""The safety gate. Nothing is applied unless it passes here on every sample."""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from pydantic import BaseModel, ValidationError

from aegis_healer.agent.privacy import KEY_PLACEHOLDER, is_data_like_key
from aegis_healer.mapping import apply_mappings
from aegis_healer.paths import has_path, model_paths
from aegis_healer.types import FieldMapping, MappingPatch, PatchCheck


def _covered(loc: str, ignore: Iterable[str]) -> bool:
    return any(loc == t or loc.startswith(f"{t}.") or t.startswith(f"{loc}.") for t in ignore)


# Error types whose message is written by user validators and can quote the input value.
_FREE_TEXT_ERRORS = ("value_error", "assertion_error")


def _masked_loc(loc: tuple[Any, ...]) -> str:
    """Join a Pydantic loc, hiding data-like dict keys (each element is one real key)."""
    out = []
    for part in loc:
        if isinstance(part, int):
            out.append(str(part))
        elif is_data_like_key(part):
            out.append(KEY_PLACEHOLDER)
        else:
            out.append(str(part))
    return ".".join(out)


def validate_patch(
    patch: MappingPatch | list[FieldMapping],
    samples: list[dict[str, Any]],
    model: type[BaseModel],
    ignore_targets: Iterable[str] = (),
    require_sources: list[FieldMapping] | None = None,
    redact: bool = False,
) -> PatchCheck:
    """Check structure, then that every sample validates once the patch is applied.

    `ignore_targets`: fields whose validation errors are expected (declared unmappable).
    `redact`: keep payload values out of error text (for the LLM).
    `require_sources`: mappings whose sources must exist in the samples (default: all).
    Saved mappings for a source the API no longer sends are harmless (they're skipped),
    so callers pass only the new mappings here.
    """
    mappings = patch.mappings if isinstance(patch, MappingPatch) else patch
    ignore = list(ignore_targets)
    targets = model_paths(model)
    errors: list[str] = []

    for m in mappings if require_sources is None else require_sources:
        for s in m.sources:
            if not any(has_path(sample, s) for sample in samples):
                errors.append(f"source '{s}' is not in the payload")
    seen: set[str] = set()
    for m in mappings:
        if m.target not in targets:
            errors.append(f"target '{m.target}' is not a field of {model.__name__}")
        if m.target in seen:
            errors.append(f"target '{m.target}' is mapped more than once")
        seen.add(m.target)
    if errors:
        return PatchCheck(ok=False, errors=errors)

    for i, sample in enumerate(samples):
        try:
            model.model_validate(apply_mappings(sample, mappings))
        except (ValueError, KeyError) as exc:
            if not isinstance(exc, ValidationError):
                # exc text may embed the raw value; withhold it when redacting
                detail = f"{type(exc).__name__} (details withheld)" if redact else exc
                errors.append(f"sample {i}: {detail}")
                continue
            for err in exc.errors():
                loc = ".".join(str(p) for p in err["loc"])
                if _covered(loc, ignore):
                    continue
                if redact:
                    msg = err["type"] if err["type"] in _FREE_TEXT_ERRORS else err["msg"]
                    errors.append(f"sample {i}: {_masked_loc(err['loc'])}: {msg}")
                else:
                    errors.append(f"sample {i}: {loc}: {err['msg']}")
    return PatchCheck(ok=not errors, errors=errors)
