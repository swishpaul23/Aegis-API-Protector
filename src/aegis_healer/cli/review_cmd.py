"""`aegis review`, `aegis history`, `aegis rollback`."""

from __future__ import annotations

from typing import Annotated

import typer
from rich.panel import Panel
from rich.table import Table

from aegis_healer.cli.common import console, error_text, get_store, mapping_table, safe
from aegis_healer.quarantine import Quarantine
from aegis_healer.store import PendingPatch, StalePatchError
from aegis_healer.store.files import FileStore

DbOpt = Annotated[str | None, typer.Option(help="Store folder (default .aegis)")]


def _show(item: PendingPatch, details: bool = False) -> None:
    title = f"Pending fix #{item.id} · {item.model_key} · built on v{item.base_version}"
    console.print(mapping_table(item.patch.mappings, title=title))
    for u in item.patch.unmappable:
        console.print(f"[red]✗ {safe(u.target)}[/red]: {safe(u.reason)}")
    if details:
        r = item.report
        console.print(
            Panel(
                f"missing: {safe(', '.join(r.missing) or '-')}\n"
                f"unexpected: {safe(', '.join(r.unexpected) or '-')}\n"
                f"type changes: {safe(', '.join(t.path for t in r.type_mismatches) or '-')}",
                title="Drift report",
                title_align="left",
            )
        )


def _replay_hint(store: FileStore, pending_id: int) -> None:
    try:
        key = store.get_pending(pending_id).model_key
        if not Quarantine(store.root).list(key):
            return
    except (KeyError, ValueError, OSError):
        return
    module, _, name = key.rpartition(".")
    console.print(
        f"Run 'aegis replay --model {safe(module)}:{safe(name)}' to release quarantined records."
    )


def _resolve(store: FileStore, pending_id: int, approve: bool, exit_on_error: bool = True) -> bool:
    try:
        version = store.resolve_pending(pending_id, approve)
    except StalePatchError as exc:
        console.print(f"[red]✗ Fix #{pending_id} is {exc}[/red]")
        if exit_on_error:
            raise typer.Exit(1) from exc
        return False
    except (KeyError, ValueError) as exc:
        console.print(f"[red]✗ {safe(error_text(exc))}[/red]")
        if exit_on_error:
            raise typer.Exit(1) from exc
        return False
    if approve:
        console.print(f"[green]✓ Approved fix #{pending_id} → mappings v{version}[/green]")
        _replay_hint(store, pending_id)
    else:
        console.print(f"Rejected fix #{pending_id}")
    return True


def review(
    approve: Annotated[int | None, typer.Option(help="Approve this fix id")] = None,
    reject: Annotated[int | None, typer.Option(help="Reject this fix id")] = None,
    db: DbOpt = None,
) -> None:
    """Review pending fixes interactively, or approve/reject one by id."""
    store = get_store(db)
    if approve is not None or reject is not None:
        if approve is not None:
            _resolve(store, approve, True)
        if reject is not None:
            _resolve(store, reject, False)
        return

    items = store.list_pending()
    if not items:
        console.print("[green]No pending fixes.[/green]")
        return
    for item in items:
        _show(item)
        while True:
            choice = typer.prompt("[a]pprove  [r]eject  [s]kip  [d]etails", default="s").lower()
            if choice in ("d", "details"):
                _show(item, details=True)
                continue
            if choice in ("a", "approve"):
                _resolve(store, item.id, True, exit_on_error=False)
            elif choice in ("r", "reject"):
                _resolve(store, item.id, False, exit_on_error=False)
            elif choice not in ("s", "skip"):
                console.print("Type a, r, s or d.")
                continue
            break


def history(
    model_key: Annotated[str, typer.Argument(help="e.g. app.models.User")], db: DbOpt = None
) -> None:
    """Show every saved mapping version for a model."""
    versions = get_store(db).history(model_key)
    if not versions:
        console.print(f"No history for {safe(model_key)}.")
        return
    table = Table(header_style="bold")
    for col in ("Version", "Fields", "Reason", "Saved"):
        table.add_column(col)
    for v in versions:
        table.add_row(
            str(v.version),
            str(len(v.mappings)),
            safe(v.reason),
            v.created_at.strftime("%Y-%m-%d %H:%M"),
        )
    console.print(table)


def rollback(
    model_key: Annotated[str, typer.Argument(help="e.g. app.models.User")],
    version: Annotated[int, typer.Argument(help="Version to restore")],
    db: DbOpt = None,
) -> None:
    """Restore an old mapping version (saved as a new version)."""
    try:
        new = get_store(db).rollback(model_key, version)
    except KeyError as exc:
        console.print(f"[red]✗ {safe(error_text(exc))}[/red]")
        raise typer.Exit(1) from exc
    console.print(f"[green]✓ Restored v{version} as v{new}[/green]")
