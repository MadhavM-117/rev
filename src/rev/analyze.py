"""analyze subcommand — semantic code review via claude -p."""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
from typing import Optional

import typer
from rich.console import Console
from rich.panel import Panel
from rich.rule import Rule
from rich.syntax import Syntax
from rich.text import Text

from .claude import ClaudeError, ClaudeNotFoundError, ClaudeRunner

console = Console()
err_console = Console(stderr=True)

_SYSTEM_PROMPT = """\
You are a senior engineer performing a code review.
Given a unified diff, produce a structured analysis with three fields:

- intent: A single crisp sentence describing the overall purpose of this change.
- summary: 2-3 sentences of narrative context — what changed and why it matters.
- chunks: An ordered list of semantic groups, from most to least architecturally
  significant. Each chunk has:
    - title: An imperative phrase of at most 8 words.
    - explanation: 1-3 sentences describing what this chunk does and why.
    - files: The list of file paths touched by this chunk.
    - diff: The relevant unified-diff lines for this chunk, copied verbatim from the input.

Rules:
- Group changes by semantic purpose, not by file. A single chunk may span multiple files.
- Be factual. Do not speculate beyond what the diff shows.
- Do not summarize trivial whitespace or formatting changes unless they are the only change.
"""

_JSON_SCHEMA = {
    "type": "object",
    "required": ["intent", "summary", "chunks"],
    "additionalProperties": False,
    "properties": {
        "intent": {"type": "string"},
        "summary": {"type": "string"},
        "chunks": {
            "type": "array",
            "items": {
                "type": "object",
                "required": ["title", "explanation", "files", "diff"],
                "additionalProperties": False,
                "properties": {
                    "title": {"type": "string"},
                    "explanation": {"type": "string"},
                    "files": {
                        "type": "array",
                        "items": {"type": "string"},
                    },
                    "diff": {"type": "string"},
                },
            },
        },
    },
}


def _get_diff(ref: Optional[str]) -> str:
    """Acquire the diff from stdin, a git ref, or bare git diff."""
    if not sys.stdin.isatty():
        diff = sys.stdin.read()
        return diff

    if not shutil.which("git"):
        err_console.print("[red]error:[/red] git not found in PATH")
        raise typer.Exit(1)

    cmd = ["git", "diff"] if ref is None else ["git", "diff", ref]

    try:
        result = subprocess.run(cmd, capture_output=True, text=True, check=False)
    except FileNotFoundError:
        err_console.print("[red]error:[/red] git not found in PATH")
        raise typer.Exit(1)

    if result.returncode != 0:
        err_console.print(f"[red]git error:[/red] {result.stderr.strip()}")
        raise typer.Exit(1)

    return result.stdout


def _display(analysis: dict) -> None:
    intent = analysis.get("intent", "")
    summary = analysis.get("summary", "")
    chunks = analysis.get("chunks", [])

    console.print(
        Panel(
            Text(intent, style="bold"),
            title="Intent",
            border_style="cyan",
        )
    )

    console.print(
        Panel(
            summary,
            title="Summary",
            border_style="blue",
        )
    )

    for i, chunk in enumerate(chunks, 1):
        title = chunk.get("title", f"Chunk {i}")
        explanation = chunk.get("explanation", "")
        files = chunk.get("files", [])

        console.print(Rule(f"[yellow]{title}[/yellow]"))

        file_list = "\n".join(f"  [green]{f}[/green]" for f in files)
        body = explanation
        if file_list:
            body += f"\n\n{file_list}"

        console.print(Panel(body, border_style="yellow"))

        diff_text = chunk.get("diff", "")
        if diff_text:
            console.print(Syntax(diff_text, "diff", theme="ansi_dark"))


def run_analyze(
    ref: Optional[str],
    model: Optional[str],
    timeout: int,
    raw: bool,
) -> None:
    diff = _get_diff(ref)

    if not diff.strip():
        console.print("[yellow]warning:[/yellow] diff is empty — nothing to analyze")
        raise typer.Exit(0)

    runner = ClaudeRunner(model=model, timeout=timeout, max_turns=2)

    try:
        result = runner.run(
            "Analyze the following diff:",
            stdin_text=diff,
            json_schema=_JSON_SCHEMA,
            system_prompt=_SYSTEM_PROMPT,
        )
    except ClaudeNotFoundError as exc:
        err_console.print(f"[red]error:[/red] {exc}")
        raise typer.Exit(1)
    except ClaudeError as exc:
        err_console.print(f"[red]claude error:[/red] {exc}")
        raise typer.Exit(1)

    if result.structured is None:
        preview = (result.text or "")[:500]
        err_console.print("[red]error:[/red] claude did not return structured output")
        if preview:
            err_console.print(f"[dim]claude returned:[/dim]\n{preview}")
        raise typer.Exit(1)

    if raw:
        print(json.dumps(result.structured, indent=2))
        return

    _display(result.structured)
