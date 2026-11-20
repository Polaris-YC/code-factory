"""Claude Code CLI (`claude -p`) adapter."""

from __future__ import annotations

import json
from pathlib import Path

from code_factory.adapters.base import BaseAdapter
from code_factory.core.models import AgentInvocation


class ClaudeAdapter(BaseAdapter):
    adapter_type = "claude"

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
            "-p",
            "--output-format",
            "json",
            "--no-session-persistence",
        ]

        if invocation.read_only:
            cmd.extend(["--allowedTools", "Read,Glob,Grep"])
        elif self.config.get("dangerously_skip_permissions", False):
            cmd.append("--dangerously-skip-permissions")
        else:
            allowed = self.config.get(
                "allowed_tools", ["Read", "Edit", "Write", "Bash", "Glob", "Grep"]
            )
            if isinstance(allowed, list) and allowed:
                cmd.extend(["--allowedTools", ",".join(str(t) for t in allowed)])

        model = self.config.get("model")
        if model:
            cmd.extend(["--model", str(model)])

        if schema_file is not None and invocation.json_schema is not None:
            cmd.extend(["--json-schema", json.dumps(invocation.json_schema)])

        extra_args = self.config.get("extra_args", [])
        if isinstance(extra_args, list):
            cmd.extend(str(a) for a in extra_args)

        return cmd, invocation.prompt
