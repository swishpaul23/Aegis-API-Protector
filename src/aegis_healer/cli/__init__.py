"""Command-line interface: a thin wrapper over the library."""

from __future__ import annotations

import typer
from dotenv import load_dotenv

from aegis_healer.cli.check_cmd import check
from aegis_healer.cli.init_cmd import init
from aegis_healer.cli.review_cmd import history, review, rollback

app = typer.Typer(
    name="aegis",
    help="Aegis | API Healer: detect API schema drift and repair it.",
    no_args_is_help=True,
    add_completion=False,
)
app.command()(check)
app.command()(review)
app.command()(history)
app.command()(rollback)
app.command()(init)


@app.callback()
def _main() -> None:
    load_dotenv()  # CLI only: the library never reads .env on its own
