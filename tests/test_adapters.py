"""Unit tests for CLI adapters, JSON extraction, timeouts, and cross-model routing."""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

from code_factory.adapters.agy import AgyAdapter
from code_factory.adapters.base import extract_json_object
from code_factory.adapters.claude import ClaudeAdapter
from code_factory.adapters.codex import CodexAdapter
from code_factory.adapters.generic import GenericCliAdapter
from code_factory.adapters.mock import MockAdapter
from code_factory.adapters.registry import AdapterRegistry
from code_factory.core.models import AgentInvocation, AgentRole


class TestAdapters(unittest.TestCase):
    def _inv(self, tmp: str, *, read_only: bool = False) -> AgentInvocation:
        return AgentInvocation(
            prompt="Implement feature X",
            cwd=tmp,
            role=AgentRole.REVIEWER if read_only else AgentRole.WORKER,
            transcript_path=str(Path(tmp) / "transcript.log"),
            timeout_seconds=10,
            read_only=read_only,
            json_schema={"type": "object"} if read_only else None,
        )

    def test_codex_command_sandbox_and_readonly(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            adapter = CodexAdapter("codex", {"type": "codex", "command": "codex", "model": "o3"})
            cmd, stdin = adapter.build_command(
                self._inv(tmp, read_only=False),
                last_message_file=Path(tmp) / "last.txt",
            )
            self.assertEqual(stdin, "Implement feature X")
            self.assertIn("exec", cmd)
            self.assertIn("--approve-for-me", cmd)
            self.assertNotIn("-s", cmd)
            self.assertIn("-m", cmd)
            self.assertIn("o3", cmd)
            self.assertEqual(cmd[-1], "-")

            explicit_sb = CodexAdapter(
                "codex",
                {"type": "codex", "command": "codex", "approve_for_me": False, "sandbox": "workspace-write"},
            )
            sb_cmd, _ = explicit_sb.build_command(self._inv(tmp, read_only=False))
            self.assertIn("workspace-write", sb_cmd)
            self.assertNotIn("--approve-for-me", sb_cmd)

            ro_cmd, _ = adapter.build_command(
                self._inv(tmp, read_only=True),
                schema_file=Path(tmp) / "schema.json",
            )
            self.assertIn("read-only", ro_cmd)
            self.assertNotIn("--approve-for-me", ro_cmd)
            self.assertIn("--output-schema", ro_cmd)

    def test_claude_and_agy_commands(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            claude = ClaudeAdapter("claude", {"type": "claude", "command": "claude"})
            c_cmd, c_stdin = claude.build_command(self._inv(tmp, read_only=True))
            self.assertEqual(c_stdin, "Implement feature X")
            self.assertIn("-p", c_cmd)
            self.assertIn("Read,Glob,Grep", c_cmd)

            agy = AgyAdapter("agy", {"type": "agy", "command": "agy", "mode": "accept-edits"})
            a_cmd, a_stdin = agy.build_command(self._inv(tmp, read_only=False))
            self.assertIsNone(a_stdin)
            self.assertIn("--mode", a_cmd)
            self.assertIn("accept-edits", a_cmd)
            self.assertIn("--sandbox", a_cmd)
            self.assertIn("--disable-slash-commands", a_cmd)
            self.assertEqual(a_cmd[-2:], ["-p", "Implement feature X"])

            a_ro, _ = agy.build_command(
                self._inv(tmp, read_only=True),
                schema_file=Path(tmp) / "schema.json",
            )
            self.assertIn("plan", a_ro)
            self.assertIn("--json-schema", a_ro)
            self.assertEqual(a_ro[-2:], ["-p", "Implement feature X"])

    def test_generic_adapter_executes_real_subprocess_and_timeout(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            echo_adapter = GenericCliAdapter(
                "py-echo",
                {
                    "type": "generic",
                    "command": [
                        sys.executable,
                        "-c",
                        "import sys, json; p = sys.stdin.read(); print(json.dumps({'echo': p}))",
                    ],
                    "prompt_mode": "stdin",
                },
            )
            res = echo_adapter.run(self._inv(tmp))
            self.assertTrue(res.ok)
            self.assertIsNotNone(res.parsed_json)
            assert res.parsed_json is not None
            self.assertEqual(res.parsed_json["echo"], "Implement feature X")
            self.assertTrue(Path(res.transcript_path).is_file())

            slow_adapter = GenericCliAdapter(
                "py-slow",
                {
                    "type": "generic",
                    "command": [sys.executable, "-c", "import time; time.sleep(5)"],
                    "prompt_mode": "stdin",
                },
            )
            inv = AgentInvocation(
                prompt="wait",
                cwd=tmp,
                role=AgentRole.WORKER,
                transcript_path=str(Path(tmp) / "timeout.log"),
                timeout_seconds=1,
            )
            timeout_res = slow_adapter.run(inv)
            self.assertTrue(timeout_res.timed_out)
            self.assertEqual(timeout_res.exit_code, 124)

    def test_extract_json_object_variants(self) -> None:
        self.assertEqual(extract_json_object('{"a": 1}'), {"a": 1})
        fenced = 'Some prose\n```json\n{"decision": "APPROVE"}\n```\nDone.'
        self.assertEqual(extract_json_object(fenced), {"decision": "APPROVE"})
        jsonl = '{"event": "start"}\n{"decision": "REQUEST_CHANGES"}'
        self.assertEqual(extract_json_object(jsonl), {"decision": "REQUEST_CHANGES"})

        agy = AgyAdapter("agy", {"type": "agy", "command": "agy"})
        msg, parsed = agy.parse_final_message(
            '{"status":"SUCCESS","response":"{\\"decision\\":\\"APPROVE\\",\\"summary\\":\\"ok\\",\\"issues\\":[]}"}',
            "",
        )
        self.assertIn("APPROVE", msg)
        self.assertEqual(parsed, {"decision": "APPROVE", "summary": "ok", "issues": []})

    def test_registry_cross_model_picker(self) -> None:
        a1 = MockAdapter("alpha")
        a2 = MockAdapter("beta")
        reg = AdapterRegistry(
            adapters={"alpha": a1, "beta": a2},
            routing={"worker": ["alpha", "beta"], "reviewer": ["alpha", "beta"]},
        )
        worker = reg.pick_adapter("worker")
        self.assertEqual(worker.name, "alpha")

        reviewer = reg.pick_adapter("reviewer", exclude=(worker.name,))
        self.assertEqual(reviewer.name, "beta")

        # Single-adapter fallback works gracefully
        single_reg = AdapterRegistry(adapters={"alpha": a1}, routing={"reviewer": ["alpha"]})
        fallback = single_reg.pick_adapter("reviewer", exclude=("alpha",))
        self.assertEqual(fallback.name, "alpha")

        # Explicit routing["reviewer"] = ["alpha"] must NEVER silently route to unrequested "beta"
        strict_reg = AdapterRegistry(
            adapters={"alpha": a1, "beta": a2},
            routing={"worker": ["alpha"], "reviewer": ["alpha"]},
        )
        strict_rev = strict_reg.pick_adapter("reviewer", exclude=("alpha",))
        self.assertEqual(strict_rev.name, "alpha")

        # Disabled adapter is excluded by from_config
        cfg_reg = AdapterRegistry.from_config(
            adapter_configs={
                "codex": {"type": "mock", "enabled": True},
                "agy": {"type": "mock", "enabled": False},
            },
            routing={"reviewer": ["agy", "codex"]},
        )
        self.assertEqual([c.name for c in cfg_reg.probe_all()], ["codex"])


if __name__ == "__main__":
    unittest.main()
