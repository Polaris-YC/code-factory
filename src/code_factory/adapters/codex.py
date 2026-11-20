"""Codex CLI (`codex exec`) adapter."""

from __future__ import annotations

from pathlib import Path

from code_factory.adapters.base import BaseAdapter
from code_factory.core.models import AgentInvocation


class CodexAdapter(BaseAdapter):
    adapter_type = "codex"

    def build_command(
        self,
        invocation: AgentInvocation,
        *,
        last_message_file: Path | None = None,
        schema_file: Path | None = None,
    ) -> tuple[list[str], str | None]:
        cmd = [
            self.binary,
            "exec",
            "-C",
            invocation.cwd,
            "--ephemeral",
            "--json",
        ]

        if invocation.read_only:
            cmd.extend(["-s", "read-only"])
        elif self.config.get("dangerously_bypass_sandbox", False):
            cmd.append("--dangerously-bypass-approvals-and-sandbox")
        elif self.config.get("approve_for_me", True):
            cmd.append("--approve-for-me")
        else:
            sandbox = str(self.config.get("sandbox", "workspace-write"))
            cmd.extend(["-s", sandbox])

        model = self.config.get("model")
        if model:
            cmd.extend(["-m", str(model)])

        if last_message_file is not None:
            cmd.extend(["-o", str(last_message_file)])

        if schema_file is not None:
            cmd.extend(["--output-schema", str(schema_file)])

        extra_args = self.config.get("extra_args", [])
        if isinstance(extra_args, list):
            cmd.extend(str(a) for a in extra_args)

        cmd.append("-")
        return cmd, invocation.prompt
