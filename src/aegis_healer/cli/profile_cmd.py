"""`aegis profile`: what Aegis has learned about a model's fields (aggregates only)."""

from __future__ import annotations

from typing import Annotated

import typer
from rich.table import Table

from aegis_healer.cli.common import console, get_store, safe
from aegis_healer.meaning.profile import (
    BUCKETS_PER_DECADE,
    FieldProfile,
    load_profile,
    median_bucket,
)


def _dominant(counts: dict) -> str | None:
    return max(counts, key=lambda k: counts[k]) if counts else None


def _kind(f: FieldProfile) -> str:
    """The most common kind seen for the field (counts only; no values)."""
    counts = {
        "number": sum(f.numeric.values()) + f.zeros,
        "datetime": sum(f.tz.values()),
        "category": sum(f.categories.values()),
        "text": 0.0,
    }
    if f.free_text:
        counts["text"] = max(f.n - counts["number"] - counts["datetime"], 0.0)
    best = max(counts, key=lambda k: counts[k])
    return best if counts[best] > 0 else "-"


def profile(
    model_key: Annotated[str, typer.Argument(help="e.g. app.models.User")],
    db: Annotated[str | None, typer.Option(help="Store folder (default .aegis)")] = None,
) -> None:
    """Show the learned baseline per field: counts, kind, magnitude. Never raw values."""
    loaded = load_profile(get_store(db).root, model_key)
    if not loaded.fields:
        console.print(f"No profile for {safe(model_key)}.")
        return
    table = Table(title=safe(f"Profile · {model_key}"), title_justify="left", header_style="bold")
    for col in ("Field", "Records", "Kind", "Typical", "Timezone", "Categories"):
        table.add_column(col)
    for path, f in sorted(loaded.fields.items()):
        kind = _kind(f)
        mid = median_bucket(f.numeric) if kind == "number" else None
        typical = f"~10^{mid / BUCKETS_PER_DECADE:.2f}" if mid is not None else "-"
        tz = _dominant(f.tz) if kind == "datetime" else None
        cats = str(len(f.categories)) if kind == "category" else "-"
        table.add_row(safe(path), f"{f.n:g}", kind, typical, tz or "-", cats)
    console.print(table)
