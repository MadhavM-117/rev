from __future__ import annotations

from rich.console import Console
from rich.text import Text

from .models import DiffDocument, DiffLine, LineType
from .syntax import AstGrepSyntaxHighlighter


class RichDiffRenderer:
    def __init__(self, highlighter: AstGrepSyntaxHighlighter | None = None, console: Console | None = None):
        self._highlighter = highlighter or AstGrepSyntaxHighlighter()
        self._console = console or Console()

    def render_empty(self) -> None:
        self._console.print("[dim]No changes found.[/dim]")

    def render(self, doc: DiffDocument) -> None:
        for file in doc.files:
            lang = self._highlighter.lang_from_path(file.new_path)
            for hunk in file.hunks:
                for line in hunk.lines:
                    self._console.print(self._render_line(line, lang))

    def _render_line(self, line: DiffLine, lang: str | None) -> Text:
        if line.kind == LineType.ADDED:
            prefix = Text(f"{(line.new_no or 0):>6}   ", style="green")
            content = self._highlighter.build_text(
                line.text, lang=lang, base_style="#d9f2d2 on #283228"
            )
            prefix.append_text(content)
            return prefix

        if line.kind == LineType.REMOVED:
            prefix = Text(f"{(line.old_no or 0):>6}   ", style="red")
            content = self._highlighter.build_text(
                line.text, lang=lang, base_style="#f5d6d6 on #3c2828"
            )
            prefix.append_text(content)
            return prefix

        prefix = Text(f"{(line.new_no or 0):>6}   ", style="bright_blue")
        content = self._highlighter.build_text(line.text, lang=lang, base_style="bright_white")
        prefix.append_text(content)
        return prefix
