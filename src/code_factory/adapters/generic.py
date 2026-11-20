"""Declarative generic CLI adapter for plugging any coding CLI via factory.toml."""

from __future__ import annotations

from pathlib import Path

from code_factory.adapters.base import BaseAdapter
from code_factory.core.models import AgentInvocation


class GenericCliAdapter(BaseAdapter):
    adapter_type = "generic"

    def build_command(
        self,
        invocation: AgentInvocation,
        *,
        last_message_file: Path | None = None,
        schema_file: Path | None = None,
    ) -> tuple[list[str], str | None]:
        del last_message_file
        raw_cmd = self.config.get("command", [self.name])
        if isinstance(raw_cmd, str):
            template = [raw_cmd]
        else:
            template = [str(part) for part in raw_cmd]

        prompt_mode = str(self.config.get("prompt_mode", "stdin"))
        subs = {
            "{prompt}": invocation.prompt,
            "{cwd}": invocation.cwd,
            "{schema_file}": str(schema_file) if schema_file else "",
        }
        argv: list[str] = []
        for part in template:
            rendered = part
            for token, value in subs.items():
                if token in rendered:
                    rendered = rendered.replace(token, value)
            argv.append(rendered)

        cwd_flag = str(self.config.get("cwd_flag", "")).strip()
        if cwd_flag:
            argv.extend([cwd_flag, invocation.cwd])

        if prompt_mode == "arg":
            if not any("{prompt}" in part for part in template):
                argv.append(invocation.prompt)
            return argv, None
        return argv, invocation.prompt
