"""`aegis runs`: the audit trail of validate() runs."""

from __future__ import annotations

from typing import Annotated

import typer
from rich.table import Table

from aegis_healer.cli.common import console, get_store, safe
from aegis_healer.runlog import read_runs

DbOpt = Annotated[str | None, typer.Option(help="Store folder (default .aegis)")]


def runs(
    limit: Annotated[int, typer.Option(help="How many recent runs to show")] = 20,
    db: DbOpt = None,
) -> None:
    """Show recent runs, newest first."""
    items = read_runs(get_store(db).root, limit)
    if not items:
        console.print("No runs recorded yet.")
        return
    table = Table(header_style="bold")
    for col in ("Time", "Model", "Status", "Records", "Fixed fields", "LLM calls", "Cost"):
        table.add_column(col)
    for r in items:
        table.add_row(
            r.at.strftime("%Y-%m-%d %H:%M:%S"),
            safe(r.model_key),
            r.status,
            str(r.records),
            safe(", ".join(r.fields_fixed) or "-"),
            str(r.usage.calls),
            f"~${r.usage.est_cost_usd:.4f}",
        )
    console.print(table)
