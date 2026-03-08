"""rev — Claude-backed code review CLI."""

from __future__ import annotations

from typing import Annotated, Optional

import typer

from .diff import run_diff

app = typer.Typer(
    name="rev",
    rich_markup_mode="rich",
    no_args_is_help=True,
    help="Streamline code review for humans.",
)


@app.callback()
def _() -> None:
    pass


@app.command("diff")
def diff(
    ref: Annotated[
        Optional[str],
        typer.Argument(
            help="Git ref to diff against (e.g. HEAD~1). Omit to diff working tree."
        ),
    ] = None,
) -> None:
    """Analyze a diff and return a structured semantic code review.

    \b
    Diff sources (in priority order):
      1. ref arg — rev analyze HEAD~1
      2. bare    — rev analyze  (diffs the working tree)
    """
    run_diff(ref=ref)
