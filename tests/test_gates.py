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
    PermissionGateConfig,
    SlopGateConfig,
)
from code_factory.gates.arch import run_arch_gate
from code_factory.gates.permissions import run_permission_gate
from code_factory.gates.runner import GateRunner
from code_factory.gates.slop import run_slop_gate


class TestGates(unittest.TestCase):
    def test_slop_gate_catches_scope_and_forbidden_patterns(self) -> None:
        cfg = SlopGateConfig(
            enabled=True,
            max_diff_lines=50,
            forbid_patterns=[r"\bREVIEW_MARKER\b"],
            enforce_owned_paths=True,
        )
        diff = (
            "diff --git a/src/allowed.py b/src/allowed.py\n"
            "+++ b/src/allowed.py\n"
            "+x = 1  # REVIEW_MARKER\n"
            "diff --git a/src/secret.py b/src/secret.py\n"
            "+++ b/src/secret.py\n"
            "+y = 2\n"
            "diff --git a/README.md b/README.md\n"
            "+++ b/README.md\n"
            "+# REVIEW_MARKER\n"
        )
        res = run_slop_gate(
            changed_files=["src/allowed.py", "src/secret.py", "README.md"],
            diff_text=diff,
            config=cfg,
            owned_paths=["src/allowed.py", "README.md"],
        )
        self.assertFalse(res.passed)
        self.assertIn("Out-of-scope", res.excerpt)
        self.assertIn("matched in src/allowed.py", res.excerpt)
        self.assertIn("matched in README.md", res.excerpt)

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

    def test_slop_gate_excludes_markdown_from_comment_ratio(self) -> None:
        cfg = SlopGateConfig(enabled=True, max_diff_lines=100)
        markdown_diff = "+++ b/README.md\n+# Setup\n" + "".join(
            f"+* Step {i}\n" for i in range(39)
        )
        res = run_slop_gate(
            changed_files=["README.md"],
            diff_text=markdown_diff,
            config=cfg,
        )
        self.assertTrue(res.passed, res.excerpt)

        code_diff = "+++ b/src/a.py\n" + "".join(
            f"+# Explanation {i}\n+x_{i} = {i}\n" for i in range(10)
        )
        res = run_slop_gate(
            changed_files=["README.md", "src/a.py"],
            diff_text=markdown_diff + code_diff,
            config=cfg,
        )
        self.assertFalse(res.passed)
        self.assertIn("Comment-to-code ratio on added lines (0.50)", res.excerpt)

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


    def test_slop_gate_deletion_bias_does_not_penalize_large_deletions(self) -> None:
        cfg = SlopGateConfig(enabled=True, max_diff_lines=50)
        removed_block = "\n".join(f"-old line {i}" for i in range(1800))
        diff = f"+++ b/src/a.py\n+x = 1\n{removed_block}\n"
        res = run_slop_gate(
            changed_files=["src/a.py"],
            diff_text=diff,
            config=cfg,
            owned_paths=["src/*.py"],
        )
        self.assertTrue(res.passed)

    def test_arch_gate_catches_forbidden_capability_calls_and_aliases(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            pkg = root / "src" / "demo"
            (pkg / "domain").mkdir(parents=True)
            (pkg / "domain" / "bad.py").write_text(
                "import subprocess as sp\n"
                "from subprocess import Popen as Spawn\n"
                "def run_stuff():\n"
                "    sp.run(['echo', 'hi'])\n"
                "    Spawn(['ls'])\n"
                "    eval('1+1')\n",
                encoding="utf-8",
            )
            cfg = ArchGateConfig(
                enabled=True,
                src_root="src",
                rules=[
                    ArchRuleConfig(
                        from_module="demo.domain",
                        forbid_calls=["subprocess.*", "eval"],
                    )
                ],
            )
            res = run_arch_gate(root, cfg)
            self.assertFalse(res.passed)
            self.assertIn("subprocess.run", res.excerpt)
            self.assertIn("subprocess.Popen", res.excerpt)
            self.assertIn("eval", res.excerpt)

    def test_permission_gate_enforces_protected_default_deny_rename_delete_and_symlinks(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            cfg = PermissionGateConfig(
                enabled=True,
                default_deny=True,
                protected_paths=[".code-factory/**", ".github/**"],
                allow_delete=False,
                allow_symlinks=False,
            )

            # Deny always wins even if owned_paths attempts to include protected path.
            res_protected = run_permission_gate(
                cwd=root,
                changed_files=[".code-factory/factory.toml"],
                diff_text="",
                config=cfg,
                owned_paths=[".code-factory/factory.toml"],
            )
            self.assertFalse(res_protected.passed)
            self.assertIn("Protected path modification denied", res_protected.excerpt)

            # Default-deny rejects empty owned_paths when files are modified.
            res_empty = run_permission_gate(
                cwd=root,
                changed_files=["src/app.py"],
                diff_text="",
                config=cfg,
                owned_paths=[],
            )
            self.assertFalse(res_empty.passed)
            self.assertIn("Default-deny violation", res_empty.excerpt)

            # Rename + delete + symlink mode in diff are caught.
            diff = (
                "diff --git a/src/outside.py b/src/allowed.py\n"
                "rename from src/outside.py\n"
                "rename to src/allowed.py\n"
                "diff --git a/src/allowed_del.py b/src/allowed_del.py\n"
                "deleted file mode 100644\n"
                "diff --git a/src/link.py b/src/link.py\n"
                "new file mode 120000\n"
            )
            res_ops = run_permission_gate(
                cwd=root,
                changed_files=["src/allowed.py", "src/allowed_del.py", "src/link.py"],
                diff_text=diff,
                config=cfg,
                owned_paths=["src/allowed*.py", "src/link.py"],
            )
            self.assertFalse(res_ops.passed)
            self.assertIn("src/outside.py", res_ops.excerpt)
            self.assertIn("File deletion denied", res_ops.excerpt)
            self.assertIn("Symlink creation denied", res_ops.excerpt)

            # Traversal path rejected.
            res_trav = run_permission_gate(
                cwd=root,
                changed_files=["../escape.py"],
                diff_text="",
                config=cfg,
                owned_paths=["src/*.py"],
            )
            self.assertFalse(res_trav.passed)
            self.assertIn("Path traversal", res_trav.excerpt)

    def test_permission_gate_blocks_symlink_escape_even_when_symlinks_allowed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "repo"
            root.mkdir()
            outside = Path(tmp) / "outside.txt"
            outside.write_text("secret", encoding="utf-8")
            (root / "src").mkdir()
            (root / "src" / "escape.txt").symlink_to(outside)

            cfg = PermissionGateConfig(
                enabled=True,
                default_deny=True,
                protected_paths=[],
                allow_symlinks=True,
            )
            res = run_permission_gate(
                cwd=root,
                changed_files=["src/escape.txt"],
                diff_text="",
                config=cfg,
                owned_paths=["src/**"],
            )
            self.assertFalse(res.passed)
            self.assertIn("Symlink escape denied", res.excerpt)


if __name__ == "__main__":
    unittest.main()
