from __future__ import annotations

import re
from typing import Optional

from .models import DiffDocument, DiffFile, DiffLine, Hunk, LineType


class UnifiedDiffParser:
    HUNK_RE = re.compile(r"@@ -(\d+)(?:,\d+)? \+(\d+)(?:,\d+)? @@")
    NEW_PATH_RE = re.compile(r"^\+\+\+ b/(.+)$")
    OLD_PATH_RE = re.compile(r"^--- a/(.+)$")

    def parse(self, diff_text: str) -> DiffDocument:
        doc = DiffDocument()

        current_file: Optional[DiffFile] = None
        current_hunk: Optional[Hunk] = None
        old_line = 0
        new_line = 0

        for line in diff_text.splitlines():
            if line.startswith("diff --git "):
                current_file = DiffFile()
                doc.files.append(current_file)
                current_hunk = None
                old_line = 0
                new_line = 0
                continue

            if current_file is None:
                continue

            if line.startswith("--- "):
                old_match = self.OLD_PATH_RE.match(line)
                if old_match:
                    current_file.old_path = old_match.group(1)
                continue

            if line.startswith("+++ "):
                new_match = self.NEW_PATH_RE.match(line)
                if new_match:
                    current_file.new_path = new_match.group(1)
                continue

            if line.startswith("@@"):
                match = self.HUNK_RE.match(line)
                if not match:
                    current_hunk = None
                    continue

                old_line = int(match.group(1))
                new_line = int(match.group(2))
                current_hunk = Hunk(old_start=old_line, new_start=new_line)
                current_file.hunks.append(current_hunk)
                continue

            if current_hunk is None:
                continue

            if line.startswith("+") and not line.startswith("+++"):
                current_hunk.lines.append(
                    DiffLine(kind=LineType.ADDED, text=line[1:], old_no=None, new_no=new_line)
                )
                new_line += 1
            elif line.startswith("-") and not line.startswith("---"):
                current_hunk.lines.append(
                    DiffLine(kind=LineType.REMOVED, text=line[1:], old_no=old_line, new_no=None)
                )
                old_line += 1
            else:
                context_text = line[1:] if line.startswith(" ") else line
                current_hunk.lines.append(
                    DiffLine(
                        kind=LineType.CONTEXT,
                        text=context_text,
                        old_no=old_line,
                        new_no=new_line,
                    )
                )
                old_line += 1
                new_line += 1

        return doc
