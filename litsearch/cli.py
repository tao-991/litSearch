"""Command-line entry point. Commands are added as the pipeline is built (SPEC §9)."""

from __future__ import annotations

from typing import Annotated

import typer

from litsearch import __version__

app = typer.Typer(
    help="Deep, multi-source, auditable literature search.",
    no_args_is_help=True,
    add_completion=False,
)


def _version_callback(value: bool) -> None:
    if value:
        typer.echo(f"litsearch {__version__}")
        raise typer.Exit()


@app.callback()
def main(
    version: Annotated[
        bool,
        typer.Option(
            "--version",
            callback=_version_callback,
            is_eager=True,
            help="Show the version and exit.",
        ),
    ] = False,
) -> None:
    """Deep, multi-source, auditable literature search."""
