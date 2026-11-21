"""`aegis replay`: release quarantined records after their fix was approved."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Annotated

import typer

from aegis_healer.cli.common import console, error_text, get_store, load_model, safe
from aegis_healer.healer import Healer
from aegis_healer.paths import model_key
from aegis_healer.quarantine import Quarantine, apply_replay, atomic_write, discard_batches
from aegis_healer.quarantine import replay as run_replay


def _left(quarantine: Quarantine, batch_ids: list[str]) -> int:
    """Records in batches that could not be removed."""
    ids = set(batch_ids)
    return sum(len(b.records) for b in quarantine.list() if b.id in ids)


def replay(
    model: Annotated[str, typer.Option(help="Pydantic model, e.g. app.models:User")],
    batch: Annotated[str | None, typer.Option(help="Only this quarantine batch id")] = None,
    out: Annotated[
        Path | None, typer.Option(help="Write released records here (JSON Lines), then remove")
    ] = None,
    discard_dead: Annotated[
        bool, typer.Option("--discard-dead", help="Delete batches that belong to rejected fixes")
    ] = False,
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
    try:
        result = run_replay(quarantine, healer, cls, batch, remove=False)
    except (ValueError, OSError) as exc:  # the text can hold payload: show the type only
        console.print(f"[red]✗ Replay failed ({type(exc).__name__}).[/red]")
        raise typer.Exit(1) from exc

    # The same record can sit in several batches: release it once.
    records, seen = [], set()
    for r in result.released:
        dump = r.model_dump_json()
        if dump not in seen:
            seen.add(dump)
            records.append(r)
    released = len(records)
    failed: list[str] = []
    if not released:
        console.print("Nothing to release.")
    elif out is None:
        console.print(f"Would release {released} record(s).")
    else:
        if out.exists():
            console.print(
                f"[red]✗ {safe(out)} exists; choose a new file (nothing was removed).[/red]"
            )
            raise typer.Exit(1)
        lines = [json.dumps(r.model_dump(mode="json")) + "\n" for r in records]
        try:
            atomic_write(out, "".join(lines))  # saved before anything is removed
        except OSError as exc:
            console.print(f"[red]✗ Could not write {safe(out)}: {safe(error_text(exc))}[/red]")
            raise typer.Exit(1) from exc
        failed = apply_replay(quarantine, result)
        console.print(f"Released {released} record(s) to {safe(out)}.")
    console.print(f"Still blocked: {result.still_blocked} record(s).")
    if released and out is None:
        console.print(
            "Nothing removed. Re-run with --out FILE to save released records, "
            "or use Pipeline to load them."
        )
    if failed:
        ids = ", ".join(safe(b) for b in failed)
        console.print(
            f"[red]✗ Could not update batch(es) {ids}; "
            "re-running will export their records again.[/red]"
        )
    if result.still_blocked and not released:
        console.print("Approve the pending fix first: aegis review")
    elif result.still_blocked:
        console.print(
            "The rest still fail with the current mappings and stay quarantined "
            "(see aegis review for open fixes)."
        )
    if result.dead_batches:
        if discard_dead:
            dead = set(result.dead_batches)
            count = sum(len(b.records) for b in quarantine.list(model_key(cls)) if b.id in dead)
            not_removed = discard_batches(quarantine, result.dead_batches)
            console.print(f"Discarded {count - _left(quarantine, not_removed)} record(s).")
            failed += not_removed
        else:
            console.print(
                f"{len(result.dead_batches)} batch(es) belong to rejected fixes; "
                "use --discard-dead to delete them."
            )
    if failed or result.still_blocked:
        raise typer.Exit(1)
