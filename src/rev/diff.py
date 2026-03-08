import re

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

        for line in diff_text.splitlines():
            if line.startswith("@@"):
                match = re.match(r"@@ -(\d+)(?:,\d+)? \+(\d+)(?:,\d+)? @@", line)
                if match:
                    old_line = int(match.group(1))
                    new_line = int(match.group(2))
                continue

            if line.startswith(("diff --git", "index ", "--- ", "+++ ")):
                continue

            if line.startswith("+") and not line.startswith("+++"):
                prefix = Text(f"{new_line:>6}   ", style="green")
                content = Text(line[1:], style="black on bright_green")
                prefix.append_text(content)
                console.print(prefix)
                new_line += 1
            elif line.startswith("-") and not line.startswith("---"):
                prefix = Text(f"{old_line:>6}   ", style="red")
                content = Text(line[1:], style="black on bright_red")
                prefix.append_text(content)
                console.print(prefix)
                old_line += 1
            else:
                if old_line and new_line:
                    prefix = Text(f"{new_line:>6}   ", style="blue")
                    content = Text(line, style="dim")
                    prefix.append_text(content)
                    console.print(prefix)
                    old_line += 1
                    new_line += 1
