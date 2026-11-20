"""`aegis init`: generate a starting Pydantic model from a live API response."""

from __future__ import annotations

from pathlib import Path
from typing import Annotated

import typer

from aegis_healer.cli.common import console, fetch_json, parse_headers
from aegis_healer.codegen import generate_models
from aegis_healer.paths import get_path


def init(
    url: Annotated[str, typer.Argument(help="API endpoint returning JSON")],
    name: Annotated[str, typer.Option(help="Name of the top-level model")] = "Model",
    out: Annotated[Path, typer.Option(help="File to write")] = Path("models.py"),
    records_path: Annotated[str | None, typer.Option(help="Dotted path to the records")] = None,
    header: Annotated[list[str] | None, typer.Option("--header", "-H")] = None,
    force: Annotated[bool, typer.Option(help="Overwrite an existing file")] = False,
) -> None:
    """Write a Pydantic model inferred from the API's current response."""
    if out.exists() and not force:
        console.print(f"[red]✗ {out} exists. Use --force to overwrite.[/red]")
        raise typer.Exit(1)
    try:
        payload = fetch_json(url, parse_headers(header or []))
    except Exception as exc:
        console.print(f"[red]Could not fetch {url}:[/red] {exc}")
        raise typer.Exit(2) from exc
    data = get_path(payload, records_path) if records_path else payload
    records = data if isinstance(data, list) else [data]
    try:
        source = generate_models(records, name)
    except ValueError as exc:
        console.print(f"[red]✗ {exc}[/red]")
        raise typer.Exit(1) from exc
    out.write_text(source, encoding="utf-8")
    console.print(f"[green]✓ Wrote {out}[/green] from {len(records)} record(s).")
    console.print(f"Next: aegis check {url} --model {out.stem}:{name}")
