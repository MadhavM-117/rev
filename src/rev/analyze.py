"""analyze subcommand — semantic code review via claude -p."""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import termios
import tty
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import typer
from ast_grep_py import SgRoot
from rich.align import Align
from rich.console import Console
from rich.panel import Panel
from rich.rule import Rule
from rich.syntax import Syntax
from rich.text import Text

from .claude import ClaudeError, ClaudeNotFoundError, ClaudeRunner

console = Console()
err_console = Console(stderr=True)

_KEY_RIGHT = "\x1b[C"
_KEY_LEFT = "\x1b[D"

_DEFAULT_MODEL = "claude-haiku-4-5"

_EXT_TO_RICH_LANG: dict[str, str] = {
    ".py": "python", ".js": "javascript", ".jsx": "jsx",
    ".ts": "typescript", ".tsx": "typescript",
    ".go": "go", ".rs": "rust", ".java": "java",
    ".c": "c", ".h": "c", ".cpp": "cpp", ".cc": "cpp",
    ".hpp": "cpp", ".cxx": "cpp", ".rb": "ruby",
    ".kt": "kotlin", ".swift": "swift", ".cs": "csharp",
    ".sh": "bash",
}

_EXT_TO_ASTGREP_LANG: dict[str, str] = {
    ".py": "python",   ".js": "javascript",  ".jsx": "javascript",
    ".ts": "typescript", ".tsx": "tsx",
    ".go": "go",       ".rs": "rust",        ".java": "java",
    ".c": "c",         ".h": "c",            ".cpp": "cpp",
    ".cc": "cpp",      ".hpp": "cpp",        ".cxx": "cpp",
    ".rb": "ruby",     ".kt": "kotlin",      ".swift": "swift",
    ".cs": "csharp",   ".sh": "bash",
}

_SCOPE_KINDS: dict[str, list[str]] = {
    "python":     ["function_definition", "class_definition"],
    "javascript": ["function_declaration", "function_expression", "arrow_function",
                   "class_declaration", "method_definition"],
    "typescript": ["function_declaration", "function_expression", "arrow_function",
                   "class_declaration", "method_definition"],
    "tsx":        ["function_declaration", "function_expression", "arrow_function",
                   "class_declaration", "method_definition"],
    "go":         ["function_declaration", "method_declaration"],
    "rust":       ["function_item", "impl_item"],
    "java":       ["method_declaration", "class_declaration", "constructor_declaration"],
    "c":          ["function_definition"],
    "cpp":        ["function_definition"],
    "ruby":       ["method", "singleton_method", "class"],
    "kotlin":     ["function_declaration", "class_declaration"],
    "swift":      ["function_declaration", "class_declaration"],
    "csharp":     ["method_declaration", "class_declaration", "constructor_declaration"],
    "bash":       ["function_definition"],
}

_SYSTEM_PROMPT = """\
You are a senior engineer performing a code review.
Given a unified diff, produce a structured analysis with three fields:

- intent: A single crisp sentence describing the overall purpose of this change.
- summary: 2-3 sentences of narrative context — what changed and why it matters.
- chunks: An ordered list of fine-grained semantic units, from most to least architecturally
  significant. Each chunk has:
    - title: An imperative phrase of at most 8 words.
    - explanation: 1-3 sentences describing what this chunk does and why.
    - files: The list of file paths touched by this chunk.
    - diff: The relevant unified-diff lines for this chunk, copied verbatim from the input.

Rules:
- Chunk at the HIGHEST possible resolution. Each chunk should represent one coherent,
  atomic semantic idea — a single function added, a single behaviour changed, a single
  data structure modified, a single import reorganised, etc.
- A single file MUST produce multiple chunks whenever it contains multiple distinct
  semantic changes. Never group all edits from one file into a single chunk.
- A chunk may span multiple files only when the changes are truly inseparable (e.g. an
  interface definition and its sole implementation changed together).
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


@dataclass
class _FileDiff:
    additions: set[int] = field(default_factory=set)
    deletions_at: dict[int, list[str]] = field(default_factory=dict)

    @property
    def changed_lines(self) -> set[int]:
        return self.additions | self.deletions_at.keys()


def _parse_diff_details(diff_text: str) -> dict[str, _FileDiff]:
    """Parse a unified diff into {filepath: _FileDiff}."""
    result: dict[str, _FileDiff] = {}
    current_file: str | None = None
    current_diff: _FileDiff | None = None
    new_cursor = 0

    def _flush() -> None:
        if current_file is None or current_diff is None:
            return
        if current_file in result:
            result[current_file].additions |= current_diff.additions
            for pos, texts in current_diff.deletions_at.items():
                result[current_file].deletions_at.setdefault(pos, []).extend(texts)
        else:
            result[current_file] = current_diff

    for line in diff_text.splitlines():
        if line.startswith("+++ "):
            _flush()
            path = line[4:]
            if path.startswith("b/"):
                path = path[2:]
            current_file = None if path == "/dev/null" else path
            current_diff = _FileDiff() if current_file else None
            new_cursor = 0
        elif line.startswith("@@ "):
            m = re.search(r'\+(\d+)', line)
            if m:
                new_cursor = int(m.group(1))
        elif current_file is not None and current_diff is not None:
            if line.startswith("\\"):
                continue  # skip "\ No newline at end of file" marker
            if line.startswith("+"):
                current_diff.additions.add(new_cursor)
                new_cursor += 1
            elif line.startswith("-"):
                current_diff.deletions_at.setdefault(new_cursor, []).append(line[1:])
                # don't advance new_cursor — deleted lines don't exist in new file
            else:
                new_cursor += 1

    _flush()
    return result


def _find_enclosing_scope_range(filepath: str, changed_lines: set[int]) -> tuple[int, int, str] | None:
    """Return (start_1, end_1, rich_lang) for the innermost enclosing scope, or None."""
    repo_root = _repo_root()
    if not repo_root or not changed_lines:
        return None
    abs_path = Path(repo_root) / filepath
    try:
        source = abs_path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None

    ext = Path(filepath).suffix.lower()
    ag_lang = _EXT_TO_ASTGREP_LANG.get(ext)
    if ag_lang is None:
        return None
    rich_lang = _EXT_TO_RICH_LANG.get(ext, "text")

    try:
        sg_root = SgRoot(source, ag_lang)
    except Exception:
        return None

    root_node = sg_root.root()
    candidates: list[tuple[int, int, int]] = []
    for kind in _SCOPE_KINDS.get(ag_lang, []):
        for scope_node in root_node.find_all(kind=kind):
            rng = scope_node.range()
            start_1 = rng.start.line + 1
            end_1 = rng.end.line + 1
            span = end_1 - start_1
            if set(range(start_1, end_1 + 1)) & changed_lines:
                candidates.append((start_1, end_1, span))

    if not candidates:
        return None

    # Innermost = smallest span
    candidates.sort(key=lambda t: t[2])
    start_1, end_1, _ = candidates[0]
    return start_1, end_1, rich_lang


def _build_interleaved_diff(
    source_lines: list[str],
    scope_start: int,
    scope_end: int,
    file_diff: _FileDiff,
) -> str:
    """Produce a diff-formatted string interleaving scope source with +/- lines."""
    out: list[str] = []
    for line_num in range(scope_start, scope_end + 1):
        # First emit any deletions at this position
        for del_text in file_diff.deletions_at.get(line_num, []):
            out.append(f"-{del_text}")
        # Then emit the source line
        if line_num <= len(source_lines):
            src = source_lines[line_num - 1]  # 1-indexed to 0-indexed
            if line_num in file_diff.additions:
                out.append(f"+{src}")
            else:
                out.append(f" {src}")
    # Trailing deletions just past the scope end
    for del_text in file_diff.deletions_at.get(scope_end + 1, []):
        out.append(f"-{del_text}")
    return "\n".join(out)


def _render_interleaved_panels(chunk: dict) -> list[tuple[str, Syntax]]:
    """Return a list of (filepath, Syntax) interleaved panels for the chunk."""
    panels: list[tuple[str, Syntax]] = []
    diff_details = _parse_diff_details(chunk.get("diff", ""))
    try:
        for fp in chunk.get("files", []):
            if fp not in diff_details:
                continue
            file_diff = diff_details[fp]
            result = _find_enclosing_scope_range(fp, file_diff.changed_lines)
            if result is None:
                continue
            scope_start, scope_end, _rich_lang = result
            repo_root = _repo_root()
            if not repo_root:
                continue
            abs_path = Path(repo_root) / fp
            try:
                source = abs_path.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            source_lines = source.splitlines()
            interleaved = _build_interleaved_diff(source_lines, scope_start, scope_end, file_diff)
            panels.append((
                fp,
                Syntax(interleaved, "diff", theme="ansi_dark"),
            ))
    except Exception:
        pass
    return panels


def _cache_dir() -> Path:
    base = Path(os.environ.get("REV_CACHE_DIR", "~/.cache/rev")).expanduser()
    base.mkdir(parents=True, exist_ok=True)
    return base


def _repo_root() -> str:
    """Return the git repo root path for cache isolation, or '' if not in a repo."""
    try:
        r = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            capture_output=True, text=True, check=False,
        )
        if r.returncode == 0:
            return r.stdout.strip()
    except FileNotFoundError:
        pass
    return ""


def _cache_key(diff_text: str, model: str) -> str:
    repo = _repo_root()
    return hashlib.sha256(f"{repo}\0{model}\0{diff_text}".encode()).hexdigest()


def _load_cache(diff_text: str, model: str) -> dict | None:
    path = _cache_dir() / f"{_cache_key(diff_text, model)}.json"
    if path.exists():
        try:
            return json.loads(path.read_text())
        except (json.JSONDecodeError, OSError):
            return None
    return None


def _save_cache(diff_text: str, model: str, analysis: dict) -> None:
    try:
        path = _cache_dir() / f"{_cache_key(diff_text, model)}.json"
        path.write_text(json.dumps(analysis))
    except OSError:
        pass


def _get_diff(ref: Optional[str]) -> str:
    """Acquire the diff from stdin, a git ref, or bare git diff."""
    # If ref is explicitly provided, use git diff with that ref
    if ref is not None:
        if not shutil.which("git"):
            err_console.print("[red]error:[/red] git not found in PATH")
            raise typer.Exit(1)
        cmd = ["git", "diff", ref]
        try:
            result = subprocess.run(cmd, capture_output=True, text=True, check=False)
        except FileNotFoundError:
            err_console.print("[red]error:[/red] git not found in PATH")
            raise typer.Exit(1)
        if result.returncode != 0:
            err_console.print(f"[red]git error:[/red] {result.stderr.strip()}")
            raise typer.Exit(1)
        return result.stdout

    # No ref provided - check if stdin has actual data to read
    # Use select to check for readable data without blocking
    if not sys.stdin.isatty():
        import select
        # Check if stdin has data available (timeout=0 means don't block)
        readable, _, _ = select.select([sys.stdin], [], [], 0)
        if readable:
            diff = sys.stdin.read()
            return diff

    # Fall back to git diff of working tree
    if not shutil.which("git"):
        err_console.print("[red]error:[/red] git not found in PATH")
        raise typer.Exit(1)

    try:
        result = subprocess.run(["git", "diff"], capture_output=True, text=True, check=False)
    except FileNotFoundError:
        err_console.print("[red]error:[/red] git not found in PATH")
        raise typer.Exit(1)

    if result.returncode != 0:
        err_console.print(f"[red]git error:[/red] {result.stderr.strip()}")
        raise typer.Exit(1)

    return result.stdout


@contextmanager
def _raw_mode(tty_file):
    fd = tty_file.fileno()
    old = termios.tcgetattr(fd)
    try:
        tty.setcbreak(fd)
        yield
    finally:
        termios.tcsetattr(fd, termios.TCSAFLUSH, old)


def _read_key(tty_file) -> str:
    ch = tty_file.read(1)
    if ch == "\x1b":
        ch += tty_file.read(2)
    return ch


def _render_chunk(idx: int, chunks: list, analysis: dict, con: Console) -> None:
    intent = analysis.get("intent", "")
    summary = analysis.get("summary", "")
    total = len(chunks)
    chunk = chunks[idx]

    title = chunk.get("title", f"Chunk {idx + 1}")
    explanation = chunk.get("explanation", "")
    files = chunk.get("files", [])
    diff_text = chunk.get("diff", "")

    con.clear()

    con.print(
        Panel(
            Text(intent, style="bold"),
            title="Intent",
            border_style="cyan",
        )
    )

    con.print(
        Panel(
            Text(summary),
            title="Summary",
            border_style="blue",
        )
    )

    body = Text(explanation)
    if files:
        body.append("\n\n")
        body.append("\n".join(f"  {f}" for f in files), style="green")

    con.print(
        Panel(
            body,
            title=f"[bold]{title}[/bold]  [dim]({idx + 1}/{total})[/dim]",
            border_style="cyan",
        )
    )

    interleaved = _render_interleaved_panels(chunk)

    con.print(Rule())

    if interleaved:
        for fp, syntax_obj in interleaved:
            con.print(Panel(syntax_obj, title=f"[dim]{fp}[/dim]", border_style="dim"))
    elif diff_text:
        con.print(Syntax(diff_text, "diff", theme="ansi_dark"))

    con.print(Rule())

    if total == 1:
        nav = "[dim]q quit[/dim]"
    else:
        left = "[dim]←[/dim]" if idx == 0 else "[bold]←[/bold]"
        right = "[dim]→[/dim]" if idx == total - 1 else "[bold]→[/bold]"
        nav = f"{left} prev   {right} next   [dim]q quit[/dim]"

    con.print(Align.center(nav))


def _interactive_display(analysis: dict) -> None:
    if not sys.stdout.isatty():
        _display(analysis)
        return

    chunks = analysis.get("chunks", [])
    if not chunks:
        console.print("[yellow]No chunks to display.[/yellow]")
        return

    try:
        tty_file = open("/dev/tty", "r")
    except OSError:
        _display(analysis)
        return

    idx = 0
    try:
        with _raw_mode(tty_file):
            while True:
                _render_chunk(idx, chunks, analysis, console)
                key = _read_key(tty_file)
                if key in ("q", "Q", "\x03"):
                    break
                elif key == _KEY_RIGHT and idx < len(chunks) - 1:
                    idx += 1
                elif key == _KEY_LEFT and idx > 0:
                    idx -= 1
    except KeyboardInterrupt:
        pass
    finally:
        tty_file.close()

    console.clear()


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

        interleaved = _render_interleaved_panels(chunk)
        if interleaved:
            for fp, syntax_obj in interleaved:
                console.print(Panel(syntax_obj, title=f"[dim]{fp}[/dim]", border_style="dim"))
        else:
            diff_text = chunk.get("diff", "")
            if diff_text:
                console.print(Syntax(diff_text, "diff", theme="ansi_dark"))


def _count_diff_changes(diff_text: str) -> int:
    """Count the number of +/- content lines in a unified diff."""
    count = 0
    for line in diff_text.splitlines():
        if line.startswith("+++ ") or line.startswith("--- "):
            continue
        if line.startswith("+") or line.startswith("-"):
            count += 1
    return count


def run_analyze(
    ref: Optional[str],
    model: Optional[str],
    timeout: int,
    raw: bool,
    debug: bool = False,
    cache: bool = True,
) -> None:
    diff = _get_diff(ref)

    if not diff.strip():
        console.print("[yellow]warning:[/yellow] diff is empty — nothing to analyze")
        raise typer.Exit(0)

    effective_model = model or _DEFAULT_MODEL
    cache_model = effective_model

    if cache and not raw:
        cached = _load_cache(diff, cache_model)
        if cached is not None:
            if debug:
                err_console.print("[dim]debug: cache hit[/dim]")
            _interactive_display(cached)
            return
        if debug:
            err_console.print("[dim]debug: cache miss[/dim]")

    runner = ClaudeRunner(model=effective_model, timeout=timeout, max_turns=2, debug=debug)

    try:
        if debug:
            result = runner.run(
                "Analyze the following diff:",
                stdin_text=diff,
                json_schema=_JSON_SCHEMA,
                system_prompt=_SYSTEM_PROMPT,
            )
        else:
            with console.status("[dim]Analyzing…[/dim]", spinner="dots"):
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

    original_changes = _count_diff_changes(diff)
    chunk_changes = sum(
        _count_diff_changes(c.get("diff", ""))
        for c in result.structured.get("chunks", [])
    )
    if chunk_changes < original_changes:
        pct = round(100 * chunk_changes / original_changes) if original_changes else 0
        err_console.print(
            f"[yellow]warning:[/yellow] chunks cover {pct}% of changes "
            f"({chunk_changes}/{original_changes} lines)"
        )

    if debug:
        cost = f"${result.cost_usd:.4f}" if result.cost_usd else "n/a"
        dur  = f"{result.duration_ms / 1000:.1f}s" if result.duration_ms else "n/a"
        err_console.print(f"[dim]debug: done  cost={cost}  duration={dur}[/dim]")

    if raw:
        print(json.dumps(result.structured, indent=2))
        return

    if cache:
        _save_cache(diff, cache_model, result.structured)

    _interactive_display(result.structured)
