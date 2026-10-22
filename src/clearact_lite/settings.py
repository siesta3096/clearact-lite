"""Explicit local settings; no private configuration is copied from other projects."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path


@dataclass(frozen=True)
class Settings:
    workspace: Path
    base_url: str = "http://localhost:1235/v1"
    model: str = "qwen/qwen3.8-27b"
    api_key: str = field(default="", repr=False)
    context_tokens: int = 32768
    max_output_tokens: int = 8192
    tool_result_chars: int = 6000
    request_timeout_seconds: float = 180
    run_timeout_seconds: float = 600
    reasoning_effort: str | None = "none"
    temperature: float = 0.1
    auto_compact: bool = True
    compact_threshold: float = 0.9
    compact_target: float = 0.6
    auto_continue: bool = True

    def __post_init__(self) -> None:
        if not 0 < self.max_output_tokens < self.context_tokens:
            raise ValueError("Output allowance must be positive and smaller than context")
        if self.tool_result_chars < 512:
            raise ValueError("Tool excerpts must allow at least 512 characters")
        if self.request_timeout_seconds <= 0 or self.run_timeout_seconds <= 0:
            raise ValueError("Timeouts must be positive")
        if not 0 < self.compact_target < self.compact_threshold < 1:
            raise ValueError("Compaction target must be below its threshold, both between zero and one")

    @classmethod
    def from_env(cls, workspace: Path) -> Settings:
        return cls(
            workspace=workspace.resolve(),
            base_url=os.getenv("CLEARACT_BASE_URL", "http://localhost:1235/v1"),
            model=os.getenv("CLEARACT_MODEL", "qwen/qwen3.8-27b"),
            api_key=os.getenv("CLEARACT_API_KEY", ""),
            context_tokens=int(os.getenv("CLEARACT_CONTEXT_TOKENS", "32768")),
            max_output_tokens=int(os.getenv("CLEARACT_MAX_OUTPUT_TOKENS", "8192")),
            reasoning_effort=os.getenv("CLEARACT_REASONING_EFFORT", "none") or None,
            auto_compact=os.getenv("CLEARACT_AUTO_COMPACT", "true").lower() == "true",
            compact_threshold=float(os.getenv("CLEARACT_COMPACT_THRESHOLD", "0.9")),
            compact_target=float(os.getenv("CLEARACT_COMPACT_TARGET", "0.6")),
            auto_continue=os.getenv("CLEARACT_AUTO_CONTINUE", "true").lower() == "true",
        )
