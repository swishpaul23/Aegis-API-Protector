"""Command-line interface: a thin wrapper over the library."""

from __future__ import annotations

import sys

import typer
from dotenv import load_dotenv

from aegis_healer.cli.check_cmd import check
from aegis_healer.cli.init_cmd import init
from aegis_healer.cli.replay_cmd import replay
from aegis_healer.cli.review_cmd import history, review, rollback
from aegis_healer.cli.runs_cmd import runs

app = typer.Typer(
    name="aegis",
    help="Aegis | API Healer: detect API schema drift and repair it.",
    no_args_is_help=True,
    add_completion=False,
    pretty_exceptions_show_locals=False,  # never dump locals (headers, keys) on a crash
)
app.command()(check)
app.command()(review)
app.command()(history)
app.command()(rollback)
app.command()(init)
app.command()(runs)
app.command()(replay)


@app.callback()
def _main() -> None:
    load_dotenv()  # CLI only: the library never reads .env on its own
    # Legacy Windows consoles (cp1252) can't print ✓ ✗: replace rather than crash.
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            try:
                reconfigure(errors="replace")
            except (ValueError, OSError):
                pass
