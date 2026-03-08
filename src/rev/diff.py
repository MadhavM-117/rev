import re
from pathlib import Path

import ast_grep_py as sg
from git import Repo
from rich.console import Console
from rich.text import Text


def run_diff(ref=None):
    d = _Differ()
    d.pretty_print(ref)


class _Differ:
    """
    Internal helper class to interact with git diffs
    """

    _HUNK_RE = re.compile(r"@@ -(\d+)(?:,\d+)? \+(\d+)(?:,\d+)? @@")
    _DIFF_PATH_RE = re.compile(r"^\+\+\+ b/(.+)$")
    _EXT_TO_LANG = {
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
    _PY_KEYWORDS = {
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

    def pretty_print(self, ref=None):
        """
        Display the git diff
        """
        repo = Repo(search_parent_directories=True)
        console = Console()

        diff_text = repo.git.diff() if ref is None else repo.git.diff(ref)

        if not diff_text.strip():
            console.print("[dim]No changes found.[/dim]")
            return

        old_line = 0
        new_line = 0
        current_lang = None

        for line in diff_text.splitlines():
            if line.startswith("@@"):
                match = self._HUNK_RE.match(line)
                if match:
                    old_line = int(match.group(1))
                    new_line = int(match.group(2))
                continue

            if line.startswith("+++ "):
                current_lang = self._lang_from_diff_header(line)
                continue

            if line.startswith(("diff --git", "index ", "--- ")):
                continue

            if line.startswith("+") and not line.startswith("+++"):
                prefix = Text(f"{new_line:>6}   ", style="green")
                content = self._build_syntax_text(
                    line[1:], lang=current_lang, base_style="#d9f2d2 on #283228"
                )
                prefix.append_text(content)
                console.print(prefix)
                new_line += 1
            elif line.startswith("-") and not line.startswith("---"):
                prefix = Text(f"{old_line:>6}   ", style="red")
                content = self._build_syntax_text(
                    line[1:], lang=current_lang, base_style="#f5d6d6 on #3c2828"
                )
                prefix.append_text(content)
                console.print(prefix)
                old_line += 1
            elif old_line and new_line:
                prefix = Text(f"{new_line:>6}   ", style="bright_blue")
                context_line = line[1:] if line.startswith(" ") else line
                content = self._build_syntax_text(
                    context_line, lang=current_lang, base_style="bright_white"
                )
                prefix.append_text(content)
                console.print(prefix)
                old_line += 1
                new_line += 1

    def _lang_from_diff_header(self, line):
        match = self._DIFF_PATH_RE.match(line)
        if not match:
            return None
        path = match.group(1)
        return self._EXT_TO_LANG.get(Path(path).suffix.lower())

    def _build_syntax_text(self, source, lang=None, base_style=""):
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

    def _style_for_leaf(self, node):
        kind = node.kind()
        token = node.text()
        parent = node.parent()
        parent_kind = parent.kind() if parent else ""

        # Pi dark-theme inspired syntax palette (higher contrast on dark diff backgrounds)
        if kind in {"comment", "line_comment", "block_comment"}:
            return "#6A9955"
        if kind in {"string", "string_start", "string_content", "string_end"}:
            return "#CE9178"
        if kind in {"integer", "float", "number"}:
            return "#B5CEA8"
        if kind in {"true", "false", "none", "null"} or token in {"True", "False", "None", "null"}:
            return "#B5CEA8"
        if token in self._PY_KEYWORDS:
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
