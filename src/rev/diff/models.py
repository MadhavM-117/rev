from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum


class LineType(str, Enum):
    ADDED = "added"
    REMOVED = "removed"
    CONTEXT = "context"


@dataclass(slots=True)
class DiffLine:
    kind: LineType
    text: str
    old_no: int | None = None
    new_no: int | None = None


@dataclass(slots=True)
class Hunk:
    old_start: int
    new_start: int
    lines: list[DiffLine] = field(default_factory=list)


@dataclass(slots=True)
class DiffFile:
    old_path: str | None = None
    new_path: str | None = None
    language: str | None = None
    hunks: list[Hunk] = field(default_factory=list)


@dataclass(slots=True)
class DiffDocument:
    files: list[DiffFile] = field(default_factory=list)

    @property
    def is_empty(self) -> bool:
        return all(not f.hunks for f in self.files)
