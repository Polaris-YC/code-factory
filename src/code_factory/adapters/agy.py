"""Antigravity CLI (`agy -p`) adapter."""

from __future__ import annotations

from pathlib import Path

from code_factory.adapters.base import BaseAdapter
from code_factory.core.models import AgentInvocation


class AgyAdapter(BaseAdapter):
    adapter_type = "agy"

    def build_command(
        self,
        invocation: AgentInvocation,
        *,
        last_message_file: Path | None = None,
        schema_file: Path | None = None,
    ) -> tuple[list[str], str | None]:
        del last_message_file
        cmd = [
            self.binary,
            "--output-format",
            "json",
            "--disable-slash-commands",
        ]

        if invocation.read_only:
            cmd.extend(["--mode", "plan", "--sandbox"])
        elif self.config.get("dangerously_skip_permissions", False):
            cmd.append("--dangerously-skip-permissions")
        else:
            mode = str(self.config.get("mode", "accept-edits"))
            cmd.extend(["--mode", mode])
            if self.config.get("sandbox", True):
                cmd.append("--sandbox")

        model = self.config.get("model")
        if model:
            cmd.extend(["--model", str(model)])

        if schema_file is not None:
            cmd.extend(["--json-schema", str(schema_file)])

        extra_args = self.config.get("extra_args", [])
        if isinstance(extra_args, list):
            cmd.extend(str(a) for a in extra_args)

        cmd.extend(["-p", invocation.prompt])
        return cmd, None
