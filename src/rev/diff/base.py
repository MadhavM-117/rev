from __future__ import annotations

from abc import ABC

from .parser import UnifiedDiffParser
from .renderer import RichDiffRenderer
from .source import GitDiffSource


class BaseDiffer(ABC):
    def __init__(self, source, parser, renderer):
        self._source = source
        self._parser = parser
        self._renderer = renderer

    def pretty_print(self, ref=None) -> None:
        diff_text = self._source.get_diff(ref=ref)
        if not diff_text.strip():
            self._renderer.render_empty()
            return

        doc = self._parser.parse(diff_text)
        self._renderer.render(doc)


class GitRichDiffer(BaseDiffer):
    def __init__(self):
        super().__init__(
            source=GitDiffSource(),
            parser=UnifiedDiffParser(),
            renderer=RichDiffRenderer(),
        )
