"""Claude Agent SDK wrapper for non-interactive analysis."""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass, field
from typing import Any

from claude_agent_sdk import (
    ClaudeAgentOptions,
    CLINotFoundError as SDKCLINotFoundError,
    ClaudeSDKError,
    ProcessError,
    ResultMessage,
    query,
)


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
        try:
            return asyncio.run(
                asyncio.wait_for(
                    self._run_async(
                        prompt,
                        stdin_text=stdin_text,
                        json_schema=json_schema,
                        system_prompt=system_prompt,
                    ),
                    timeout=self.timeout,
                )
            )
        except asyncio.TimeoutError as exc:
            raise ClaudeError(
                f"claude timed out after {self.timeout}s"
            ) from exc

    async def _run_async(
        self,
        prompt: str,
        *,
        stdin_text: str | None = None,
        json_schema: dict | None = None,
        system_prompt: str | None = None,
    ) -> ClaudeResult:
        full_prompt = prompt
        if stdin_text:
            full_prompt = f"{prompt}\n\n{stdin_text}"

        options = ClaudeAgentOptions(
            allowed_tools=[],
            max_turns=self.max_turns,
        )

        if self.model:
            options.model = self.model

        if system_prompt:
            options.system_prompt = system_prompt

        if json_schema:
            options.output_format = {"type": "json_schema", "schema": json_schema}

        try:
            result_message: ResultMessage | None = None
            async for message in query(prompt=full_prompt, options=options):
                if isinstance(message, ResultMessage):
                    result_message = message
        except SDKCLINotFoundError as exc:
            raise ClaudeNotFoundError(
                "claude binary not found in PATH. "
                "Install Claude Code: https://claude.ai/code"
            ) from exc
        except (ProcessError, ClaudeSDKError) as exc:
            raise ClaudeError(str(exc)) from exc

        if result_message is None:
            raise ClaudeError("claude produced no result message")

        if result_message.is_error:
            raise ClaudeError(
                f"claude returned an error: {result_message.result or 'unknown error'}"
            )

        structured = result_message.structured_output
        result_text = result_message.result

        if structured is None and result_text:
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
            text=result_text,
            structured=structured,
            session_id=result_message.session_id,
            cost_usd=result_message.total_cost_usd,
            duration_ms=result_message.duration_ms,
            raw={},
        )
