"""Base CLI adapter protocol, safe subprocess execution, and JSON extraction."""

from __future__ import annotations

import json
import os
import re
import shutil
import signal
import subprocess
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from code_factory.core.models import AgentInvocation, AgentResult


@dataclass(frozen=True)
class CapabilityInfo:
    name: str
    adapter_type: str
    binary: str
    available: bool
    resolved_path: str | None = None
    version: str | None = None


_JSON_BLOCK_RE = re.compile(r"```(?:json)?\s*(\{.*?\})\s*```", re.DOTALL)


def extract_json_object(text: str) -> dict[str, Any] | None:
    """Extract a JSON object from raw text, fenced JSON blocks, or JSONL."""
    stripped = text.strip()
    if not stripped:
        return None

    try:
        parsed = json.loads(stripped)
        if isinstance(parsed, dict):
            return parsed
    except json.JSONDecodeError:
        pass

    matches = _JSON_BLOCK_RE.findall(stripped)
    for candidate in reversed(matches):
        try:
            parsed = json.loads(candidate)
            if isinstance(parsed, dict):
                return parsed
        except json.JSONDecodeError:
            continue

    for line in reversed(stripped.splitlines()):
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            parsed = json.loads(line)
            if isinstance(parsed, dict):
                return parsed
        except json.JSONDecodeError:
            continue

    first = stripped.find("{")
    last = stripped.rfind("}")
    if first != -1 and last > first:
        try:
            parsed = json.loads(stripped[first : last + 1])
            if isinstance(parsed, dict):
                return parsed
        except json.JSONDecodeError:
            pass
    return None


def normalize_strict_json_schema(schema: dict[str, Any]) -> dict[str, Any]:
    """Return a copy of `schema` compatible with OpenAI/Codex strict output schemas.

    Strict structured-output validators require every object schema to set
    `"additionalProperties": false` and list all declared property keys in `required`
    (widening previously optional properties to allow `null`).
    """
    node: dict[str, Any] = dict(schema)

    props = node.get("properties")
    if isinstance(props, dict):
        original_required = set(node.get("required") or [])
        normalized_props: dict[str, Any] = {}
        for key, prop_schema in props.items():
            if isinstance(prop_schema, dict):
                child = normalize_strict_json_schema(prop_schema)
                if key not in original_required and "type" in child:
                    prop_type = child["type"]
                    if isinstance(prop_type, str) and prop_type != "null":
                        child["type"] = [prop_type, "null"]
                    elif isinstance(prop_type, list) and "null" not in prop_type:
                        child["type"] = [*prop_type, "null"]
                normalized_props[key] = child
            else:
                normalized_props[key] = prop_schema
        node["properties"] = normalized_props
        node["required"] = list(normalized_props.keys())
        node.setdefault("additionalProperties", False)
    elif node.get("type") == "object":
        node.setdefault("additionalProperties", False)

    items = node.get("items")
    if isinstance(items, dict):
        node["items"] = normalize_strict_json_schema(items)

    return node


class BaseAdapter(ABC):
    """Abstract base class for all CLI coding agent adapters."""

    adapter_type: str = "base"

    def __init__(self, name: str, config: dict[str, Any]) -> None:
        self.name = name
        self.config = dict(config)

    @property
    def binary(self) -> str:
        cmd = self.config.get("command", self.name)
        if isinstance(cmd, list) and cmd:
            return str(cmd[0])
        return str(cmd)

    def probe(self) -> CapabilityInfo:
        resolved = shutil.which(self.binary)
        version: str | None = None
        if resolved:
            try:
                proc = subprocess.run(
                    [resolved, "--version"],
                    capture_output=True,
                    text=True,
                    timeout=5,
                    check=False,
                )
                out = (proc.stdout or proc.stderr).strip().splitlines()
                for line in out:
                    if line.strip() and not line.startswith("WARNING:"):
                        version = line.strip()[:120]
                        break
            except (OSError, subprocess.SubprocessError):
                version = None
        return CapabilityInfo(
            name=self.name,
            adapter_type=self.adapter_type,
            binary=self.binary,
            available=resolved is not None,
            resolved_path=resolved,
            version=version,
        )

    @abstractmethod
    def build_command(
        self,
        invocation: AgentInvocation,
        *,
        last_message_file: Path | None = None,
        schema_file: Path | None = None,
    ) -> tuple[list[str], str | None]:
        """Return (argv, stdin_payload) for executing this invocation."""

    def parse_final_message(
        self,
        stdout: str,
        stderr: str,
        last_message_file: Path | None = None,
    ) -> tuple[str, dict[str, Any] | None]:
        if last_message_file and last_message_file.is_file():
            content = last_message_file.read_text(encoding="utf-8", errors="replace").strip()
            if content:
                return content, extract_json_object(content)
        parsed = extract_json_object(stdout)
        if parsed:
            for envelope_key in ("result", "response"):
                inner = parsed.get(envelope_key)
                if isinstance(inner, str):
                    nested = extract_json_object(inner)
                    return inner, nested or parsed
        return stdout.strip(), parsed

    def run(self, invocation: AgentInvocation) -> AgentResult:
        transcript = Path(invocation.transcript_path)
        transcript.parent.mkdir(parents=True, exist_ok=True)

        last_message_file = transcript.with_suffix(".last_message.txt")
        schema_file: Path | None = None
        if invocation.json_schema is not None:
            schema_file = transcript.with_suffix(".schema.json")
            normalized_schema = normalize_strict_json_schema(invocation.json_schema)
            schema_file.write_text(
                json.dumps(normalized_schema, indent=2),
                encoding="utf-8",
            )

        argv, stdin_payload = self.build_command(
            invocation,
            last_message_file=last_message_file,
            schema_file=schema_file,
        )

        env = os.environ.copy()
        env.update(invocation.extra_env)

        start = time.monotonic()
        timed_out = False
        stdout = ""
        stderr = ""
        exit_code = -1

        proc = subprocess.Popen(
            argv,
            cwd=invocation.cwd,
            stdin=subprocess.PIPE if stdin_payload is not None else subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env=env,
            start_new_session=True,
        )
        try:
            stdout, stderr = proc.communicate(
                input=stdin_payload,
                timeout=invocation.timeout_seconds,
            )
            exit_code = proc.returncode
        except subprocess.TimeoutExpired:
            timed_out = True
            try:
                os.killpg(proc.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
            try:
                stdout, stderr = proc.communicate(timeout=5)
            except subprocess.TimeoutExpired:
                try:
                    os.killpg(proc.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                stdout, stderr = proc.communicate()
            exit_code = 124

        duration = round(time.monotonic() - start, 3)
        transcript.write_text(
            f"# Adapter: {self.name} ({self.adapter_type})\n"
            f"# Command: {' '.join(argv)}\n"
            f"# Exit: {exit_code} (timed_out={timed_out}, duration={duration}s)\n"
            f"\n--- STDOUT ---\n{stdout}\n"
            f"\n--- STDERR ---\n{stderr}\n",
            encoding="utf-8",
        )

        final_msg, parsed_json = self.parse_final_message(
            stdout, stderr, last_message_file=last_message_file
        )
        return AgentResult(
            adapter=self.name,
            exit_code=exit_code,
            stdout=stdout,
            stderr=stderr,
            final_message=final_msg,
            duration_seconds=duration,
            transcript_path=str(transcript),
            timed_out=timed_out,
            parsed_json=parsed_json,
        )
