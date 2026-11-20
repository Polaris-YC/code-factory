"""Deterministic mock adapter for offline E2E testing and dry-run verification."""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

from code_factory.adapters.base import BaseAdapter, CapabilityInfo
from code_factory.core.models import AgentInvocation, AgentResult

MockHandler = Callable[[AgentInvocation, int], tuple[int, str, dict[str, Any] | None]]


class MockAdapter(BaseAdapter):
    adapter_type = "mock"

    def __init__(
        self,
        name: str,
        config: dict[str, Any] | None = None,
        handler: MockHandler | None = None,
    ) -> None:
        super().__init__(name=name, config=config or {"type": "mock"})
        self._handler = handler
        self.call_count = 0
        self.invocations: list[AgentInvocation] = []

    def probe(self) -> CapabilityInfo:
        return CapabilityInfo(
            name=self.name,
            adapter_type=self.adapter_type,
            binary="mock",
            available=True,
            resolved_path="mock://in-memory",
            version="mock-1.0",
        )

    def build_command(
        self,
        invocation: AgentInvocation,
        *,
        last_message_file: Path | None = None,
        schema_file: Path | None = None,
    ) -> tuple[list[str], str | None]:
        del invocation, last_message_file, schema_file
        return ["mock"], None

    def run(self, invocation: AgentInvocation) -> AgentResult:
        self.call_count += 1
        self.invocations.append(invocation)
        transcript = Path(invocation.transcript_path)
        transcript.parent.mkdir(parents=True, exist_ok=True)

        if self._handler is not None:
            exit_code, message, parsed = self._handler(invocation, self.call_count)
        else:
            parsed = {
                "decision": "APPROVE",
                "summary": f"Mock {self.name} approved.",
                "issues": [],
            }
            message = json.dumps(parsed)
            exit_code = 0

        transcript.write_text(
            f"# Adapter: {self.name} (mock)\n# Call: {self.call_count}\n"
            f"# Role: {invocation.role.value}\n\n{message}\n",
            encoding="utf-8",
        )
        return AgentResult(
            adapter=self.name,
            exit_code=exit_code,
            stdout=message,
            stderr="",
            final_message=message,
            duration_seconds=0.01,
            transcript_path=str(transcript),
            timed_out=False,
            parsed_json=parsed,
        )
