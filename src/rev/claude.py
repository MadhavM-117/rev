"""Subprocess wrapper for claude -p (non-interactive pipe mode)."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from dataclasses import dataclass, field
from typing import Any


class ClaudeNotFoundError(Exception):
    """Raised when the claude binary is not found in PATH."""


class ClaudeError(Exception):
    """Raised on non-zero exit, timeout, or invalid output from claude."""


@dataclass
class ClaudeResult:
    text: str | None
    structured: Any
    session_id: str | None
    cost_usd: float | None
    duration_ms: int | None
    raw: dict = field(default_factory=dict)


class ClaudeRunner:
    def __init__(
        self,
        model: str | None = None,
        max_turns: int = 1,
        timeout: int = 120,
    ) -> None:
        self.model = model
        self.max_turns = max_turns
        self.timeout = timeout

    def run(
        self,
        prompt: str,
        *,
        stdin_text: str | None = None,
        json_schema: dict | None = None,
        system_prompt: str | None = None,
    ) -> ClaudeResult:
        if not shutil.which("claude"):
            raise ClaudeNotFoundError(
                "claude binary not found in PATH. "
                "Install Claude Code: https://claude.ai/code"
            )

        cmd = [
            "claude",
            "--print",
            "--output-format", "json",
            "--max-turns", str(self.max_turns),
        ]

        if self.model:
            cmd += ["--model", self.model]

        if system_prompt:
            cmd += ["--system-prompt", system_prompt]

        if json_schema:
            cmd += ["--json-schema", json.dumps(json_schema)]

        cmd.append(prompt)

        env = {k: v for k, v in os.environ.items() if k != "CLAUDECODE"}

        try:
            result = subprocess.run(
                cmd,
                input=stdin_text,
                capture_output=True,
                text=True,
                timeout=self.timeout,
                env=env,
            )
        except subprocess.TimeoutExpired as exc:
            raise ClaudeError(
                f"claude timed out after {self.timeout}s"
            ) from exc

        if result.returncode != 0:
            stderr = result.stderr.strip()
            raise ClaudeError(
                f"claude exited with code {result.returncode}"
                + (f": {stderr}" if stderr else "")
            )

        raw_output = result.stdout.strip()
        if not raw_output:
            raise ClaudeError("claude produced no output")

        try:
            raw = json.loads(raw_output)
        except json.JSONDecodeError as exc:
            raise ClaudeError(
                f"claude output is not valid JSON: {exc}\nOutput: {raw_output[:200]}"
            ) from exc

        structured = raw.get("structured_output")
        if structured is None:
            result_text = raw.get("result", "")
            if result_text:
                text = result_text.strip()
                if text.startswith("```"):
                    first_nl = text.find("\n")
                    if first_nl != -1:
                        text = text[first_nl + 1:]
                    if text.endswith("```"):
                        text = text[:-3].strip()
                try:
                    structured = json.loads(text)
                except json.JSONDecodeError:
                    pass

        return ClaudeResult(
            text=raw.get("result"),
            structured=structured,
            session_id=raw.get("session_id"),
            cost_usd=raw.get("cost_usd"),
            duration_ms=raw.get("duration_ms"),
            raw=raw,
        )
