"""`aegis replay`: release quarantined records after their fix was approved."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Annotated

import typer

from aegis_healer.cli.common import console, error_text, get_store, load_model, safe
from aegis_healer.healer import Healer
from aegis_healer.paths import model_key
from aegis_healer.quarantine import Quarantine, atomic_write, remove_batches
from aegis_healer.quarantine import replay as run_replay


def replay(
    model: Annotated[str, typer.Option(help="Pydantic model, e.g. app.models:User")],
    batch: Annotated[str | None, typer.Option(help="Only this quarantine batch id")] = None,
    out: Annotated[
        Path | None, typer.Option(help="Write released records here (JSON Lines), then remove")
    ] = None,
    db: Annotated[str | None, typer.Option(help="Store folder (default .aegis)")] = None,
) -> None:
    """Re-check quarantined records with the current mappings and release the valid ones."""
    cls = load_model(model)
    store = get_store(db)
    quarantine = Quarantine(store.root)
    if batch is not None:
        try:
            found = quarantine.load(batch).model_key == model_key(cls)
        except ValueError:
            found = False
        if not found:
            console.print(f"[red]✗ No quarantine batch {safe(batch)} for {safe(model)}.[/red]")
            raise typer.Exit(1)
    healer = Healer(store=store, proposer=None)  # replay never calls the LLM
    result = run_replay(quarantine, healer, cls, batch, remove=False)

    if out is None:
        console.print(f"Would release {len(result.released)} record(s).")
    else:
        lines = [json.dumps(r.model_dump(mode="json")) + "\n" for r in result.released]
        try:
            atomic_write(out, "".join(lines))  # saved before anything is removed
        except OSError as exc:
            console.print(f"[red]✗ Could not write {safe(out)}: {safe(error_text(exc))}[/red]")
            raise typer.Exit(1) from exc
        failed = remove_batches(quarantine, result.batches_cleared)
        console.print(f"Released {len(result.released)} record(s) to {safe(out)}.")
        for batch_id in failed:
            console.print(f"[yellow]! Batch {safe(batch_id)} could not be removed.[/yellow]")
    console.print(f"Still blocked: {result.still_blocked} record(s).")
    if out is None:
        console.print(
            "Nothing removed. Re-run with --out FILE to save released records, "
            "or use Pipeline to load them."
        )
    if result.still_blocked:
        console.print("Approve the pending fix first: aegis review")
        raise typer.Exit(1)
