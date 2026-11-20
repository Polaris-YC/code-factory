"""End-to-end tests of FactoryOrchestrator with real Git repo and MockAdapters."""

from __future__ import annotations

import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from typing import Any

from code_factory.adapters.mock import MockAdapter
from code_factory.adapters.registry import AdapterRegistry
from code_factory.config import (
    ArchGateConfig,
    FactoryConfig,
    GateCommandConfig,
    SlopGateConfig,
)
from code_factory.core.models import (
    AgentInvocation,
    FactoryMode,
    TaskDAG,
    TaskSpec,
    TaskStatus,
)
from code_factory.core.orchestrator import FactoryOrchestrator
from code_factory.core.ratchet import RatchetInbox


def _init_python_repo(repo: Path) -> None:
    subprocess.run(["git", "init", "-b", "main"], cwd=str(repo), check=True, capture_output=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=str(repo), check=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=str(repo), check=True)
    (repo / "app").mkdir()
    (repo / "app" / "__init__.py").write_text("", encoding="utf-8")
    (repo / "tests").mkdir()
    (repo / "tests" / "test_smoke.py").write_text(
        "import unittest\nclass T(unittest.TestCase):\n    def test_ok(self): self.assertTrue(True)\n",
        encoding="utf-8",
    )
    subprocess.run(["git", "add", "."], cwd=str(repo), check=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=str(repo), check=True, capture_output=True)


class TestOrchestratorE2E(unittest.TestCase):
    def test_parallel_self_healing_cross_model_review_and_dark_merge(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "repo"
            repo.mkdir()
            _init_python_repo(repo)

            def worker_handler(inv: AgentInvocation, _call: int) -> tuple[int, str, dict[str, Any] | None]:
                cwd = Path(inv.cwd)
                if "Task: math-mod" in inv.prompt:
                    target = cwd / "app" / "math_ops.py"
                    if "Gate FAILED" not in inv.prompt and "Reviewer" not in inv.prompt:
                        # Attempt 1: intentionally inject forbidden pattern to trigger gate self-heal
                        target.write_text(
                            "def add(a: int, b: int) -> int:\n    # TODO(agent) clean up\n    return a + b\n",
                            encoding="utf-8",
                        )
                    elif "Reviewer" in inv.prompt:
                        # Review fix round: add multiply as requested
                        target.write_text(
                            "def add(a: int, b: int) -> int:\n    return a + b\n\n"
                            "def mul(a: int, b: int) -> int:\n    return a * b\n",
                            encoding="utf-8",
                        )
                    else:
                        # Attempt 2: fix slop gate violation
                        target.write_text(
                            "def add(a: int, b: int) -> int:\n    return a + b\n",
                            encoding="utf-8",
                        )
                elif "Task: greet-mod" in inv.prompt:
                    (cwd / "app" / "greet.py").write_text(
                        "def hello(name: str) -> str:\n    return f'Hello, {name}!'\n",
                        encoding="utf-8",
                    )
                elif "Task: wire-mod" in inv.prompt:
                    (cwd / "app" / "combined.py").write_text(
                        "from app.math_ops import add, mul\n"
                        "from app.greet import hello\n\n"
                        "def summary(name: str, x: int, y: int) -> str:\n"
                        "    return f'{hello(name)} sum={add(x, y)} prod={mul(x, y)}'\n",
                        encoding="utf-8",
                    )
                return 0, "done", None

            review_counts: dict[str, int] = {}

            def reviewer_handler(inv: AgentInvocation, _call: int) -> tuple[int, str, dict[str, Any] | None]:
                if "math-mod" in inv.prompt:
                    review_counts["math-mod"] = review_counts.get("math-mod", 0) + 1
                    if review_counts["math-mod"] == 1:
                        verdict = {
                            "decision": "REQUEST_CHANGES",
                            "summary": "Missing mul helper required by downstream contract.",
                            "issues": [
                                {
                                    "severity": "high",
                                    "file": "app/math_ops.py",
                                    "line": 1,
                                    "rule": "contract-completeness",
                                    "message": "Export mul(a, b).",
                                    "fix_hint": "Add def mul(a: int, b: int) -> int.",
                                }
                            ],
                        }
                        return 0, json.dumps(verdict), verdict
                verdict = {"decision": "APPROVE", "summary": "Clean diff, gates green.", "issues": []}
                return 0, json.dumps(verdict), verdict

            worker_mock = MockAdapter("codex", handler=worker_handler)
            reviewer_mock = MockAdapter("claude", handler=reviewer_handler)
            registry = AdapterRegistry(
                adapters={"codex": worker_mock, "claude": reviewer_mock},
                routing={"worker": ["codex"], "reviewer": ["claude", "codex"]},
            )
            config = FactoryConfig(
                mode=FactoryMode.DARK,
                max_workers=2,
                max_gate_retries=2,
                max_review_rounds=2,
                gate_commands=[
                    GateCommandConfig(
                        name="unittest",
                        cmd="python3 -m unittest discover -s tests -v",
                    )
                ],
                slop=SlopGateConfig(enabled=True, forbid_patterns=[r"TODO\(agent\)"]),
                arch=ArchGateConfig(enabled=False),
            )
            dag = TaskDAG(
                run_id="e2e-001",
                goal="Build math, greet, and combined modules",
                base_branch="main",
                mode=FactoryMode.DARK,
                tasks=[
                    TaskSpec(
                        id="math-mod",
                        title="Add math module",
                        description="Implement app/math_ops.py",
                        owned_paths=["app/math_ops.py"],
                        done_checks=["python3 -c 'from app.math_ops import add; assert add(2, 3) == 5'"],
                    ),
                    TaskSpec(
                        id="greet-mod",
                        title="Add greet module",
                        description="Implement app/greet.py",
                        owned_paths=["app/greet.py"],
                        done_checks=["python3 -c 'from app.greet import hello; assert hello(\"A\") == \"Hello, A!\"'"],
                    ),
                    TaskSpec(
                        id="wire-mod",
                        title="Wire modules together",
                        description="Implement app/combined.py using math_ops and greet",
                        depends_on=["math-mod", "greet-mod"],
                        owned_paths=["app/combined.py"],
                        done_checks=[
                            "python3 -c 'from app.combined import summary; "
                            "assert summary(\"Ada\", 2, 3) == \"Hello, Ada! sum=5 prod=6\"'"
                        ],
                    ),
                ],
            )

            orch = FactoryOrchestrator(repo_root=repo, config=config, registry=registry)
            state = orch.run_dag(dag)

            self.assertTrue(state.integrated)
            self.assertTrue(state.merged_to_base)
            for tid in ("math-mod", "greet-mod", "wire-mod"):
                self.assertEqual(state.tasks[tid].status, TaskStatus.PASSED)
                self.assertEqual(state.tasks[tid].worker_adapter, "codex")
                self.assertEqual(state.tasks[tid].reviewer_adapter, "claude")

            # math-mod failed gate once, self-healed, got 1 review rejection, then fixed & approved
            self.assertEqual(state.tasks["math-mod"].gate_attempts, 3)
            self.assertEqual(state.tasks["math-mod"].review_rounds, 2)

            # Dark mode merged all 3 files cleanly into main
            self.assertTrue((repo / "app" / "math_ops.py").is_file())
            self.assertTrue((repo / "app" / "greet.py").is_file())
            self.assertTrue((repo / "app" / "combined.py").is_file())

            # Report artifact exists and ratchet inbox recorded both gate & review mistakes
            report_path = repo / ".code-factory" / "runs" / "e2e-001" / "report.md"
            self.assertTrue(report_path.is_file())
            entries = RatchetInbox(repo).read_all()
            sources = {e.source for e in entries}
            self.assertIn("gate", sources)
            self.assertIn("review", sources)

    def test_reviewer_timeout_does_not_rerun_worker_and_resume_reuses_passed_tasks(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "repo"
            repo.mkdir()
            _init_python_repo(repo)

            worker_calls: dict[str, int] = {}

            def worker_handler(inv: AgentInvocation, _call: int) -> tuple[int, str, dict[str, Any] | None]:
                cwd = Path(inv.cwd)
                if "Task: t1" in inv.prompt:
                    worker_calls["t1"] = worker_calls.get("t1", 0) + 1
                    (cwd / "app" / "one.py").write_text("X = 1\n", encoding="utf-8")
                elif "Task: t2" in inv.prompt:
                    worker_calls["t2"] = worker_calls.get("t2", 0) + 1
                    (cwd / "app" / "two.py").write_text("Y = 2\n", encoding="utf-8")
                return 0, "ok", None

            fail_t2_reviewer = True

            def flaky_reviewer(inv: AgentInvocation, _call: int) -> tuple[int, str, dict[str, Any] | None]:
                if "t2" in inv.prompt and fail_t2_reviewer:
                    return 124, "", None
                verdict = {"decision": "APPROVE", "summary": "ok", "issues": []}
                return 0, json.dumps(verdict), verdict

            worker_mock = MockAdapter("codex", handler=worker_handler)
            reviewer_mock = MockAdapter("agy", handler=flaky_reviewer)
            reg = AdapterRegistry(
                adapters={"codex": worker_mock, "agy": reviewer_mock},
                routing={"worker": ["codex"], "reviewer": ["agy"]},
            )
            cfg = FactoryConfig(
                max_workers=2,
                max_gate_retries=2,
                max_review_rounds=2,
                gate_commands=[],
                slop=SlopGateConfig(enabled=True),
                arch=ArchGateConfig(enabled=False),
            )
            dag = TaskDAG(
                run_id="resume-001",
                goal="Test reviewer infra failure and resume",
                base_branch="main",
                tasks=[
                    TaskSpec(id="t1", title="One", description="Write app/one.py", owned_paths=["app/one.py"]),
                    TaskSpec(id="t2", title="Two", description="Write app/two.py", owned_paths=["app/two.py"]),
                ],
            )
            orch = FactoryOrchestrator(repo_root=repo, config=cfg, registry=reg)
            state1 = orch.run_dag(dag)

            self.assertFalse(state1.integrated)
            self.assertEqual(state1.tasks["t1"].status, TaskStatus.PASSED)
            self.assertEqual(state1.tasks["t2"].status, TaskStatus.ESCALATED)
            # Critical invariant: reviewer timeout must NOT send passing worker back to re-run
            self.assertEqual(worker_calls["t2"], 1)
            self.assertIsNotNone(state1.tasks["t2"].commit_sha)

            # Fix reviewer and resume: t1 must be reused without re-running worker!
            fail_t2_reviewer = False
            state2 = orch.resume_run("resume-001")
            self.assertTrue(state2.integrated)
            self.assertEqual(state2.tasks["t1"].status, TaskStatus.PASSED)
            self.assertEqual(state2.tasks["t2"].status, TaskStatus.PASSED)
            self.assertEqual(worker_calls["t1"], 1)


if __name__ == "__main__":
    unittest.main()
