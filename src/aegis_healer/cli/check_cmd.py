"""`aegis check`: fetch an API, detect drift, heal or queue the fix."""

from __future__ import annotations

from typing import Annotated

import typer

from aegis_healer.cli.common import (
    console,
    fetch_json,
    get_store,
    load_model,
    mapping_table,
    parse_headers,
)
from aegis_healer.healer import Healer

EXIT = {"ok": 0, "healed": 0, "pending": 1, "failed": 2}


def build_healer(threshold: float, db: str | None, persist: bool = True) -> Healer:
    """The single place the CLI builds a Healer (tests monkeypatch this)."""
    return Healer(store=get_store(db), auto_apply_threshold=threshold, persist=persist)


def check(
    url: Annotated[str, typer.Argument(help="API endpoint returning JSON")],
    model: Annotated[str, typer.Option("--model", "-m", help="module:ClassName of your model")],
    records_path: Annotated[
        str | None, typer.Option(help="Dotted path to the records list, e.g. data.items")
    ] = None,
    threshold: Annotated[
        float, typer.Option(min=0.0, max=1.0, help="Auto-apply agent fixes at or above this")
    ] = 0.9,
    header: Annotated[
        list[str] | None, typer.Option("--header", "-H", help="'Name: value' (repeatable)")
    ] = None,
    db: Annotated[str | None, typer.Option(help="Store folder (default .aegis)")] = None,
    dry_run: Annotated[bool, typer.Option(help="Heal in memory, save nothing")] = False,
) -> None:
    """Check an API against your model. Exit 0 ok/healed, 1 pending review, 2 failed."""
    target = load_model(model)
    headers = parse_headers(header or [])
    if headers:
        console.print("Headers: " + ", ".join(f"{k}: ***" for k in headers))
    try:
        payload = fetch_json(url, headers)
    except Exception as exc:  # network / JSON errors: report, don't traceback
        console.print(f"[red]Could not fetch {url}:[/red] {exc}")
        raise typer.Exit(2) from exc

    healer = build_healer(threshold, db, persist=not dry_run)
    if healer.proposer is None:
        console.print("[dim]Agent disabled (no ANTHROPIC_API_KEY): rules only.[/dim]")
    result = healer.validate(payload, target, records_path=records_path)

    if result.status == "ok":
        console.print(f"[green]✓ No drift.[/green] {len(result.records)} record(s) valid.")
    else:
        r = result.report
        if r:
            console.print(
                f"Drift found: {len(r.missing)} missing, {len(r.type_mismatches)} type change(s)"
            )
        if result.proposed and result.proposed.mappings:
            console.print(mapping_table(result.proposed.mappings))
        for u in result.proposed.unmappable if result.proposed else []:
            console.print(f"[red]✗ {u.target}[/red]: {u.reason}")
        if result.status == "healed":
            saved = "" if dry_run else " Saved to the store."
            console.print(f"[green]✓ Healed {len(result.records)} record(s).{saved}[/green]")
        elif result.status == "pending":
            where = (
                f" Review with: aegis review (fix #{result.pending_id})"
                if result.pending_id
                else ""
            )
            console.print(f"[yellow]Pending: {result.message}{where}[/yellow]")
        else:
            console.print(f"[red]✗ {result.message}[/red]")
    raise typer.Exit(EXIT[result.status])
