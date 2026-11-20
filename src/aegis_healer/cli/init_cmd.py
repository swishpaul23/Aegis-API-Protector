"""`aegis init`: generate a starting Pydantic model from a live API response."""

from __future__ import annotations

from pathlib import Path
from typing import Annotated

import typer

from aegis_healer.cli.common import (
    console,
    error_text,
    fetch_error_text,
    fetch_json,
    parse_headers,
    redact_url,
    safe,
)
from aegis_healer.codegen import class_name, generate_models
from aegis_healer.paths import get_path


def _module_for(out: Path) -> str:
    """Dotted module path for --model, relative to the current folder when possible."""
    try:
        rel = out.resolve().relative_to(Path.cwd().resolve())
    except ValueError:
        return out.stem
    return ".".join(rel.with_suffix("").parts)


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
        console.print(f"[red]✗ {safe(out)} exists. Use --force to overwrite.[/red]")
        raise typer.Exit(1)
    try:
        payload = fetch_json(url, parse_headers(header or []))
    except Exception as exc:
        console.print(
            f"[red]Could not fetch {safe(redact_url(url))}:[/red] {safe(fetch_error_text(exc))}"
        )
        raise typer.Exit(2) from exc
    try:
        data = get_path(payload, records_path) if records_path else payload
        records = data if isinstance(data, list) else [data]
        source = generate_models(records, name)
    except (KeyError, TypeError, ValueError) as exc:
        console.print(f"[red]✗ Could not infer a model:[/red] {safe(error_text(exc))}")
        raise typer.Exit(2) from exc
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(source, encoding="utf-8")
    console.print(f"[green]✓ Wrote {safe(out)}[/green] from {len(records)} record(s).")
    console.print(
        f"Next: aegis check {safe(redact_url(url))} --model {_module_for(out)}:{class_name(name)}"
    )
