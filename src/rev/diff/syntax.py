from __future__ import annotations

from pathlib import Path

import ast_grep_py as sg
from rich.text import Text


class AstGrepSyntaxHighlighter:
    EXT_TO_LANG = {
        ".py": "python",
        ".js": "javascript",
        ".ts": "typescript",
        ".tsx": "tsx",
        ".jsx": "jsx",
        ".rs": "rust",
        ".go": "go",
        ".java": "java",
        ".c": "c",
        ".h": "c",
        ".cpp": "cpp",
        ".hpp": "cpp",
    }

    PY_KEYWORDS = {
        "False",
        "None",
        "True",
        "and",
        "as",
        "assert",
        "async",
        "await",
        "break",
        "class",
        "continue",
        "def",
        "del",
        "elif",
        "else",
        "except",
        "finally",
        "for",
        "from",
        "global",
        "if",
        "import",
        "in",
        "is",
        "lambda",
        "nonlocal",
        "not",
        "or",
        "pass",
        "raise",
        "return",
        "try",
        "while",
        "with",
        "yield",
        "match",
        "case",
    }

    def lang_from_path(self, path: str | None) -> str | None:
        if not path:
            return None
        return self.EXT_TO_LANG.get(Path(path).suffix.lower())

    def build_text(self, source: str, lang: str | None = None, base_style: str = "") -> Text:
        text = Text(source, style=base_style)
        if not source.strip() or not lang:
            return text

        try:
            root = sg.SgRoot(source + "\n", lang).root()
        except Exception:
            return text

        stack = [root]
        while stack:
            node = stack.pop()
            children = node.children()
            if children:
                stack.extend(reversed(children))
                continue

            token_style = self._style_for_leaf(node)
            if not token_style:
                continue

            node_range = node.range()
            if node_range.start.line != 0 or node_range.end.line != 0:
                continue

            start = max(0, min(len(source), node_range.start.column))
            end = max(0, min(len(source), node_range.end.column))
            if start < end:
                text.stylize(token_style, start, end)

        return text

    def _style_for_leaf(self, node) -> str | None:
        kind = node.kind()
        token = node.text()
        parent = node.parent()
        parent_kind = parent.kind() if parent else ""

        if kind in {"comment", "line_comment", "block_comment"}:
            return "#6A9955"
        if kind in {"string", "string_start", "string_content", "string_end"}:
            return "#CE9178"
        if kind in {"integer", "float", "number"}:
            return "#B5CEA8"
        if kind in {"true", "false", "none", "null"} or token in {"True", "False", "None", "null"}:
            return "#B5CEA8"
        if token in self.PY_KEYWORDS:
            return "bold #569CD6"
        if kind in {"type_identifier", "primitive_type", "type"}:
            return "#4EC9B0"
        if kind in {"operator", "+", "-", "*", "/", "=", "==", "!=", "<", ">", "<=", ">="}:
            return "#D4D4D4"
        if kind in {"(", ")", "[", "]", "{", "}", ",", ".", ":", ";"}:
            return "#D4D4D4"
        if kind == "identifier":
            if parent_kind in {"call", "function_definition", "function_declaration", "method_definition"}:
                return "#DCDCAA"
            return "#9CDCFE"

        return None
