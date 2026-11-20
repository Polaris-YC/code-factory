"""Unit tests for deterministic gates: SlopGate, ArchGate, and GateRunner."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from code_factory.config import (
    ArchGateConfig,
    ArchRuleConfig,
    FactoryConfig,
    GateCommandConfig,
    SlopGateConfig,
)
from code_factory.gates.arch import run_arch_gate
from code_factory.gates.runner import GateRunner
from code_factory.gates.slop import run_slop_gate


class TestGates(unittest.TestCase):
    def test_slop_gate_catches_scope_and_forbidden_patterns(self) -> None:
        cfg = SlopGateConfig(
            enabled=True,
            max_diff_lines=50,
            forbid_patterns=[r"TODO\(agent\)"],
            enforce_owned_paths=True,
        )
        diff = (
            "diff --git a/src/allowed.py b/src/allowed.py\n"
            "+++ b/src/allowed.py\n"
            "+x = 1  # TODO(agent) fix later\n"
            "diff --git a/src/secret.py b/src/secret.py\n"
            "+++ b/src/secret.py\n"
            "+y = 2\n"
        )
        res = run_slop_gate(
            changed_files=["src/allowed.py", "src/secret.py"],
            diff_text=diff,
            config=cfg,
            owned_paths=["src/allowed.py"],
        )
        self.assertFalse(res.passed)
        self.assertIn("Out-of-scope", res.excerpt)
        self.assertIn("Forbidden pattern", res.excerpt)

    def test_slop_gate_passes_clean_diff(self) -> None:
        cfg = SlopGateConfig(enabled=True, max_diff_lines=100)
        diff = "+++ b/src/a.py\n+def add(a: int, b: int) -> int:\n+    return a + b\n"
        res = run_slop_gate(
            changed_files=["src/a.py"],
            diff_text=diff,
            config=cfg,
            owned_paths=["src/*.py"],
        )
        self.assertTrue(res.passed)

    def test_arch_gate_catches_forbidden_import(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            pkg = root / "src" / "demo"
            (pkg / "adapters").mkdir(parents=True)
            (pkg / "core").mkdir(parents=True)
            (pkg / "adapters" / "bad.py").write_text(
                "from demo.core.engine import run\n",
                encoding="utf-8",
            )
            cfg = ArchGateConfig(
                enabled=True,
                src_root="src",
                rules=[ArchRuleConfig(from_module="demo.adapters", forbid=["demo.core.engine"])],
            )
            res = run_arch_gate(root, cfg)
            self.assertFalse(res.passed)
            self.assertIn("demo.core.engine", res.excerpt)

    def test_gate_runner_executes_commands_and_done_checks(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            cfg = FactoryConfig(
                gate_commands=[GateCommandConfig(name="ok-cmd", cmd="python3 -c 'print(123)'")],
                slop=SlopGateConfig(enabled=True),
                arch=ArchGateConfig(enabled=False),
            )
            runner = GateRunner(cfg)
            report = runner.run_all(
                cwd=root,
                changed_files=[],
                diff_text="",
                done_checks=["python3 -c 'import sys; sys.exit(1)'"],
                log_dir=root / "logs",
            )
            self.assertFalse(report.passed)
            failed_names = [c.name for c in report.failed_checks()]
            self.assertEqual(failed_names, ["done-check-1"])
            self.assertIn("Gate FAILED: done-check-1", report.format_diagnostics())


if __name__ == "__main__":
    unittest.main()
