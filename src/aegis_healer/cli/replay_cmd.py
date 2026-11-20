"""`aegis replay`: release quarantined records after their fix was approved."""

from __future__ import annotations

from typing import Annotated

import typer

from aegis_healer.cli.common import console, error_text, get_store, load_model, safe
from aegis_healer.healer import Healer
from aegis_healer.quarantine import Quarantine
from aegis_healer.quarantine import replay as run_replay


def replay(
    model: Annotated[str, typer.Option(help="Pydantic model, e.g. app.models:User")],
    batch: Annotated[str | None, typer.Option(help="Only this quarantine batch id")] = None,
    db: Annotated[str | None, typer.Option(help="Store folder (default .aegis)")] = None,
) -> None:
    """Re-check quarantined records with the current mappings and release the valid ones."""
    cls = load_model(model)
    store = get_store(db)
    healer = Healer(store=store, proposer=None)  # replay never calls the LLM
    try:
        result = run_replay(Quarantine(store.root), healer, cls, batch)
    except (ValueError, OSError) as exc:
        console.print(f"[red]✗ {safe(error_text(exc))}[/red]")
        raise typer.Exit(1) from exc
    console.print(f"Released {len(result.released)} record(s).")
    console.print(f"Still blocked: {result.still_blocked} record(s).")
    if result.still_blocked:
        console.print("Approve the pending fix first: aegis review")
        raise typer.Exit(1)
