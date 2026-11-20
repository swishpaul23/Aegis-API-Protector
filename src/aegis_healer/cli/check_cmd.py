"""`aegis check`: fetch an API, detect drift, heal or queue the fix."""

from __future__ import annotations

from typing import Annotated, cast, get_args

import typer

from aegis_healer.agent.privacy import Privacy
from aegis_healer.cli.common import (
    console,
    error_text,
    fetch_json,
    get_store,
    load_model,
    mapping_table,
    parse_headers,
    redact_url,
    safe,
)
from aegis_healer.healer import Healer

EXIT = {"ok": 0, "healed": 0, "pending": 1, "failed": 2}


PRIVACY_MODES = get_args(Privacy)


def build_healer(
    threshold: float, db: str | None, persist: bool = True, privacy: str = "masked"
) -> Healer:
    """The single place the CLI builds a Healer (tests monkeypatch this)."""
    return Healer(
        store=get_store(db),
        auto_apply_threshold=threshold,
        persist=persist,
        privacy=cast(Privacy, privacy),
    )


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
    privacy: Annotated[
        str, typer.Option(help="What the agent sees of your data: full | masked | schema")
    ] = "masked",
) -> None:
    """Check an API against your model. Exit 0 ok/healed, 1 pending review, 2 failed."""
    if privacy not in PRIVACY_MODES:
        raise typer.BadParameter(
            f"must be one of: {', '.join(PRIVACY_MODES)}", param_hint="--privacy"
        )
    target = load_model(model)
    headers = parse_headers(header or [])
    if headers:
        console.print("Headers: " + ", ".join(f"{safe(k)}: ***" for k in headers))
    try:
        payload = fetch_json(url, headers)
    except Exception as exc:  # network / JSON errors: report, don't traceback
        console.print(f"[red]Could not fetch {safe(redact_url(url))}:[/red] {safe(exc)}")
        raise typer.Exit(2) from exc

    healer = build_healer(threshold, db, persist=not dry_run, privacy=privacy)
    if healer.proposer is None:
        console.print("[dim]Agent disabled (no ANTHROPIC_API_KEY): rules only.[/dim]")
    try:
        result = healer.validate(payload, target, records_path=records_path)
    except (KeyError, TypeError, ValueError) as exc:
        console.print(f"[red]✗ Could not read records:[/red] {safe(error_text(exc))}")
        raise typer.Exit(2) from exc

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
            console.print(f"[red]✗ {safe(u.target)}[/red]: {safe(u.reason)}")
        if result.status == "healed":
            saved = "" if dry_run else " Saved to the store."
            console.print(f"[green]✓ Healed {len(result.records)} record(s).{saved}[/green]")
        elif result.status == "pending":
            where = (
                f" Review with: aegis review (fix #{result.pending_id})"
                if result.pending_id
                else ""
            )
            console.print(f"[yellow]Pending: {safe(result.message)}{where}[/yellow]")
        else:
            console.print(f"[red]✗ {safe(result.message)}[/red]")
    raise typer.Exit(EXIT[result.status])
