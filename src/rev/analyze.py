"""analyze subcommand — semantic code review via claude -p."""

from __future__ import annotations

import ast
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
from pathlib import Path
from typing import Optional

import typer
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

_BLOCK_START_RE = re.compile(
    r'^\s*(?:'
    r'(?:export\s+)?(?:default\s+)?(?:async\s+)?function\s+\w+'
    r'|(?:export\s+)?(?:abstract\s+)?class\s+\w+'
    r'|(?:pub(?:\([^)]*\))?\s+)?(?:unsafe\s+)?(?:async\s+)?fn\s+\w+'
    r'|func\s+(?:\([^)]*\)\s+)?\w+'
    r'|(?:(?:public|private|protected|internal|static|async|override|virtual|abstract)\s+)*'
    r'  [\w<>\[\]]+\s+\w+\s*\('
    r')'
)

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


def _parse_chunk_locations(diff_text: str) -> list[tuple[str, set[int]]]:
    """Parse a unified diff and return [(filepath, {new-file line numbers of added lines})]."""
    results: list[tuple[str, set[int]]] = []
    current_file: str | None = None
    current_lines: set[int] = set()
    new_cursor = 0

    for line in diff_text.splitlines():
        if line.startswith("+++ "):
            if current_file is not None:
                results.append((current_file, current_lines))
            path = line[4:]
            if path.startswith("b/"):
                path = path[2:]
            current_file = None if path == "/dev/null" else path
            current_lines = set()
            new_cursor = 0
        elif line.startswith("@@ "):
            # @@ -old_start[,old_count] +new_start[,new_count] @@
            m = re.search(r'\+(\d+)', line)
            if m:
                new_cursor = int(m.group(1))
        elif current_file is not None:
            if line.startswith("+"):
                current_lines.add(new_cursor)
                new_cursor += 1
            elif line.startswith("-"):
                pass  # deleted line; don't advance new_cursor
            else:
                new_cursor += 1

    if current_file is not None:
        results.append((current_file, current_lines))

    return results


def _build_python_class_outline(cls_node: ast.ClassDef, changed_lines: set[int], lines: list[str]) -> str:
    """Return a collapsed class outline with full methods only where lines overlap changed_lines."""
    out: list[str] = []

    # Class header up to first body line
    header_end = cls_node.body[0].lineno - 1 if cls_node.body else cls_node.end_lineno
    out.extend(lines[cls_node.lineno - 1 : header_end])

    # Docstring if present
    first = cls_node.body[0] if cls_node.body else None
    if isinstance(first, ast.Expr) and isinstance(first.value, ast.Constant) and isinstance(first.value.value, str):
        out.extend(lines[first.lineno - 1 : first.end_lineno])
        body_nodes = cls_node.body[1:]
    else:
        body_nodes = cls_node.body

    for node in body_nodes:
        node_start = getattr(node, 'lineno', None)
        node_end = getattr(node, 'end_lineno', None)
        if node_start is None or node_end is None:
            continue
        node_range = set(range(node_start, node_end + 1))
        if node_range & changed_lines:
            # Include decorators
            deco_start = node_start
            if hasattr(node, 'decorator_list') and node.decorator_list:
                deco_start = node.decorator_list[0].lineno
            out.extend(lines[deco_start - 1 : node_end])
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            indent = len(lines[node_start - 1]) - len(lines[node_start - 1].lstrip())
            name = node.name
            out.append(" " * indent + f"def {name}(self, ...): ...")
        else:
            out.extend(lines[node_start - 1 : node_end])

    return "\n".join(out)


def _get_context_python(source: str, changed_lines: set[int]) -> tuple[str, str] | None:
    """Return (context_str, 'python') for the innermost enclosing scope in a Python file."""
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return None

    lines = source.splitlines()

    # Collect all scopes with their parent
    candidates: list[tuple[ast.AST, ast.AST | None, int]] = []  # (node, parent, span)
    for node in ast.walk(tree):
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                start = child.lineno
                end = child.end_lineno or start
                span = end - start
                node_range = set(range(start, end + 1))
                if node_range & changed_lines:
                    candidates.append((child, node, span))

    if not candidates:
        return None

    # Innermost = smallest span
    candidates.sort(key=lambda t: t[2])
    innermost, parent, _ = candidates[0]

    if isinstance(innermost, ast.ClassDef):
        ctx = _build_python_class_outline(innermost, changed_lines, lines)
        return ctx, "python"

    if isinstance(innermost, (ast.FunctionDef, ast.AsyncFunctionDef)):
        if isinstance(parent, ast.ClassDef):
            ctx = _build_python_class_outline(parent, changed_lines, lines)
            return ctx, "python"
        # Top-level function
        start = innermost.lineno - 1
        end = innermost.end_lineno or (start + 1)
        ctx = "\n".join(lines[start:end])
        return ctx, "python"

    return None


def _get_context_heuristic(source: str, changed_lines: set[int], ext: str) -> tuple[str, str] | None:
    """Return (context_str, lang) using regex + brace counting for non-Python files."""
    lang = _EXT_TO_RICH_LANG.get(ext, "text")
    lines = source.splitlines()
    if not lines or not changed_lines:
        return None

    min_line = min(changed_lines)  # 1-indexed
    search_start = min_line - 2  # 0-indexed; line just before the change

    # Scan backward up to 200 lines for a block-start
    block_start_idx: int | None = None
    for i in range(search_start, max(-1, search_start - 200), -1):
        if 0 <= i < len(lines) and _BLOCK_START_RE.match(lines[i]):
            block_start_idx = i
            break

    if block_start_idx is None:
        return None

    # Forward scan: count braces to find end
    depth = 0
    found_open = False
    block_end_idx = len(lines) - 1
    for i in range(block_start_idx, len(lines)):
        for ch in lines[i]:
            if ch == "{":
                depth += 1
                found_open = True
            elif ch == "}":
                depth -= 1
        if found_open and depth <= 0:
            block_end_idx = i
            break

    if not found_open:
        # Indentation-based or one-liner; fall back to changed_lines extent + 5
        block_end_idx = min(max(changed_lines) + 4, len(lines) - 1)

    ctx = "\n".join(lines[block_start_idx : block_end_idx + 1])
    return ctx, lang


def _get_context_for_file(filepath: str, changed_lines: set[int]) -> tuple[str, str] | None:
    """Return (context_str, lang) for the given file and changed lines, or None on any failure."""
    repo_root = _repo_root()
    if not repo_root or not changed_lines:
        return None
    abs_path = Path(repo_root) / filepath
    try:
        source = abs_path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None

    ext = Path(filepath).suffix.lower()
    if ext == ".py":
        return _get_context_python(source, changed_lines)
    if _EXT_TO_RICH_LANG.get(ext) is None:
        return None
    return _get_context_heuristic(source, changed_lines, ext)


def _render_context_panels(chunk: dict) -> list[tuple[str, Syntax]]:
    """Return a list of (filepath, Syntax) panels for the chunk's diff context."""
    panels: list[tuple[str, Syntax]] = []
    try:
        for filepath, changed_lines in _parse_chunk_locations(chunk.get("diff", "")):
            try:
                result = _get_context_for_file(filepath, changed_lines)
                if result:
                    ctx, lang = result
                    panels.append((
                        filepath,
                        Syntax(ctx, lang, theme="ansi_dark", line_numbers=True),
                    ))
            except Exception:
                pass
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

    for fp, syntax_obj in _render_context_panels(chunk):
        con.print(Panel(syntax_obj, title=f"[dim]Context: {fp}[/dim]", border_style="dim"))

    con.print(Rule())

    if diff_text:
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

        for fp, syntax_obj in _render_context_panels(chunk):
            console.print(Panel(syntax_obj, title=f"[dim]Context: {fp}[/dim]", border_style="dim"))

        diff_text = chunk.get("diff", "")
        if diff_text:
            console.print(Syntax(diff_text, "diff", theme="ansi_dark"))


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
