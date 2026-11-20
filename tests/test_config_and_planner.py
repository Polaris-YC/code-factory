"""Unit tests for layered TOML config and TaskDAG validation / planning."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from code_factory.config import load_config
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


if __name__ == "__main__":
    unittest.main()
