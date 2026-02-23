"""rev — Claude-backed code review CLI."""

from __future__ import annotations

from typing import Annotated, Optional

import typer

from .analyze import run_analyze

app = typer.Typer(
    name="rev",
    rich_markup_mode="rich",
    no_args_is_help=True,
    help="Streamline code review for humans.",
)


@app.callback()
def _() -> None:
    pass


@app.command("analyze")
def analyze(
    ref: Annotated[
        Optional[str],
        typer.Argument(help="Git ref to diff against (e.g. HEAD~1). Omit to diff working tree."),
    ] = None,
    model: Annotated[
        Optional[str],
        typer.Option("--model", "-m", envvar="REV_MODEL", help="Claude model override."),
    ] = None,
    timeout: Annotated[
        int,
        typer.Option("--timeout", "-t", envvar="REV_TIMEOUT", help="Seconds before giving up on claude."),
    ] = 120,
    raw: Annotated[
        bool,
        typer.Option("--raw", "-r", help="Dump structured JSON output instead of rendering."),
    ] = False,
    debug: Annotated[
        bool,
        typer.Option("--debug", "-d", help="Print SDK stream events to stderr."),
    ] = False,
    no_cache: Annotated[
        bool,
        typer.Option("--no-cache", help="Skip cache lookup and do not write result."),
    ] = False,
) -> None:
    """Analyze a diff and return a structured semantic code review.

    \b
    Diff sources (in priority order):
      1. stdin   — pipe a diff: git diff HEAD~1 | rev analyze
      2. ref arg — rev analyze HEAD~1
      3. bare    — rev analyze  (diffs the working tree)
    """
    run_analyze(ref=ref, model=model, timeout=timeout, raw=raw, debug=debug, cache=not no_cache)
