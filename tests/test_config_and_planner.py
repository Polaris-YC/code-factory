"""Unit tests for layered TOML config and TaskDAG validation / planning."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from code_factory.config import PermissionGateConfig, load_config
from code_factory.core.models import FactoryMode, TaskDAG, TaskSpec
from code_factory.core.planner import DAGValidationError, validate_and_plan


class TestConfigAndPlanner(unittest.TestCase):
    def test_layered_config_override(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            global_cfg = root / "global.toml"
            global_cfg.write_text(
                '[factory]\nmax_workers = 5\n[gates.slop]\nmax_diff_lines = 500\n',
                encoding="utf-8",
            )
            repo = root / "repo"
            (repo / ".code-factory").mkdir(parents=True)
            (repo / ".code-factory" / "factory.toml").write_text(
                '[factory]\nmode = "dark"\nmax_workers = 2\n',
                encoding="utf-8",
            )
            cfg = load_config(repo_root=repo, global_config_path=global_cfg)
            self.assertEqual(cfg.mode, FactoryMode.DARK)
            self.assertEqual(cfg.max_workers, 2)
            self.assertEqual(cfg.slop.max_diff_lines, 500)

    def test_valid_multi_wave_dag(self) -> None:
        dag = TaskDAG(
            run_id="r1",
            goal="Build feature",
            base_branch="main",
            tasks=[
                TaskSpec(id="a", title="A", description="", owned_paths=["src/a.py"]),
                TaskSpec(id="b", title="B", description="", owned_paths=["src/b.py"]),
                TaskSpec(
                    id="c",
                    title="C",
                    description="",
                    depends_on=["a", "b"],
                    owned_paths=["src/a.py", "src/c.py"],
                ),
            ],
        )
        plan = validate_and_plan(dag)
        self.assertEqual(len(plan.waves), 2)
        self.assertEqual([t.id for t in plan.waves[0]], ["a", "b"])
        self.assertEqual([t.id for t in plan.waves[1]], ["c"])

    def test_rejects_parallel_owned_path_overlap(self) -> None:
        dag = TaskDAG(
            run_id="r2",
            goal="Overlap",
            base_branch="main",
            tasks=[
                TaskSpec(id="a", title="A", description="", owned_paths=["src/core/**"]),
                TaskSpec(id="b", title="B", description="", owned_paths=["src/core/util.py"]),
            ],
        )
        with self.assertRaises(DAGValidationError):
            validate_and_plan(dag)

    def test_rejects_dependency_cycle(self) -> None:
        dag = TaskDAG(
            run_id="r3",
            goal="Cycle",
            base_branch="main",
            tasks=[
                TaskSpec(id="a", title="A", description="", depends_on=["b"], owned_paths=["a.py"]),
                TaskSpec(id="b", title="B", description="", depends_on=["a"], owned_paths=["b.py"]),
            ],
        )
        with self.assertRaises(DAGValidationError):
            validate_and_plan(dag)


    def test_rejects_invalid_done_check_shell_syntax(self) -> None:
        dag = TaskDAG(
            run_id="r4",
            goal="Broken quote in done_check",
            base_branch="main",
            tasks=[
                TaskSpec(
                    id="a",
                    title="A",
                    description="",
                    owned_paths=["a.py"],
                    done_checks=['python3 -c "print(\'unterminated)'],
                ),
            ],
        )
        with self.assertRaises(DAGValidationError) as ctx:
            validate_and_plan(dag)
        self.assertIn("invalid shell syntax", str(ctx.exception))

    def test_static_permission_verifier_rejects_escalation_empty_and_traversal(self) -> None:
        perms = PermissionGateConfig(
            enabled=True,
            default_deny=True,
            protected_paths=[".code-factory/**", ".github/**"],
        )
        # 1. Non-escalation: owned_paths overlapping protected_paths must fail before run.
        dag_escalate = TaskDAG(
            run_id="r5",
            goal="Modify gate config",
            base_branch="main",
            tasks=[
                TaskSpec(
                    id="t1",
                    title="Escalate",
                    description="",
                    owned_paths=[".code-factory/factory.toml"],
                )
            ],
        )
        with self.assertRaises(DAGValidationError) as ctx:
            validate_and_plan(dag_escalate, permissions=perms)
        self.assertIn("non-escalation invariant", str(ctx.exception))

        # 2. Empty owned_paths rejected under default_deny.
        dag_empty = TaskDAG(
            run_id="r6",
            goal="Empty scope",
            base_branch="main",
            tasks=[TaskSpec(id="t1", title="Empty", description="", owned_paths=[])],
        )
        with self.assertRaises(DAGValidationError) as ctx2:
            validate_and_plan(dag_empty, permissions=perms)
        self.assertIn("empty owned_paths", str(ctx2.exception))

        # 3. Path traversal in owned_paths rejected.
        dag_trav = TaskDAG(
            run_id="r7",
            goal="Traversal",
            base_branch="main",
            tasks=[TaskSpec(id="t1", title="Trav", description="", owned_paths=["../outside.py"])],
        )
        with self.assertRaises(DAGValidationError) as ctx3:
            validate_and_plan(dag_trav, permissions=perms)
        self.assertIn("Path traversal", str(ctx3.exception))

    def test_loads_permissions_and_arch_forbid_calls_from_toml(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "repo"
            (repo / ".code-factory").mkdir(parents=True)
            (repo / ".code-factory" / "factory.toml").write_text(
                '[gates.permissions]\n'
                'enabled = true\n'
                'default_deny = true\n'
                'protected_paths = [".code-factory/**", "secrets/**"]\n'
                'allow_delete = false\n'
                'allow_symlinks = false\n'
                '[gates.arch]\n'
                'enabled = true\n'
                '[[gates.arch.rules]]\n'
                'from = "demo.core"\n'
                'forbid = ["demo.cli"]\n'
                'forbid_calls = ["subprocess.*", "eval"]\n',
                encoding="utf-8",
            )
            cfg = load_config(repo_root=repo, global_config_path=Path(tmp) / "missing.toml")
            self.assertTrue(cfg.permissions.enabled)
            self.assertFalse(cfg.permissions.allow_delete)
            self.assertEqual(cfg.permissions.protected_paths, [".code-factory/**", "secrets/**"])
            self.assertEqual(cfg.arch.rules[0].forbid_calls, ["subprocess.*", "eval"])


if __name__ == "__main__":
    unittest.main()
