"""Offline unit tests for architect-guided parallel full-repo audit (`cf audit`)."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from typing import Any

from code_factory.adapters.mock import MockAdapter
from code_factory.adapters.registry import AdapterRegistry
from code_factory.config import FactoryConfig
from code_factory.core.auditor import RepoAuditor, build_deterministic_shards, scan_repo_files
from code_factory.core.models import AgentInvocation


class TestRepoAuditor(unittest.TestCase):
    def _make_sample_repo(self, root: Path) -> Path:
        repo = root / "factor_repo"
        (repo / "src" / "factors").mkdir(parents=True)
        (repo / "src" / "engine").mkdir(parents=True)
        (repo / "tests").mkdir(parents=True)
        (repo / "src" / "factors" / "mom.py").write_text(
            "def momentum(xs):\n    return xs\n", encoding="utf-8"
        )
        (repo / "src" / "engine" / "runner.py").write_text(
            "def run():\n    return 1\n", encoding="utf-8"
        )
        (repo / "tests" / "test_mom.py").write_text(
            "def test_ok():\n    assert True\n", encoding="utf-8"
        )
        return repo

    def test_scan_and_deterministic_sharding(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = self._make_sample_repo(Path(tmp))
            files = scan_repo_files(repo)
            paths = [p for p, _ in files]
            self.assertEqual(
                paths,
                ["src/engine/runner.py", "src/factors/mom.py", "tests/test_mom.py"],
            )
            plan = build_deterministic_shards(files)
            self.assertEqual(len(plan.shards), 3)

    def test_lead_guided_parallel_audit_and_synthesis(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = self._make_sample_repo(Path(tmp))

            def lead_handler(inv: AgentInvocation, _call: int) -> tuple[int, str, dict[str, Any] | None]:
                self.assertTrue(inv.read_only)
                if "Audit Director" in inv.prompt:
                    plan = {
                        "repo_summary": "Small factor engine with factors and engine layers.",
                        "global_risks": ["Lookahead bias in factor transforms"],
                        "shards": [
                            {
                                "id": "factors",
                                "title": "Factor definitions",
                                "paths": ["src/factors/mom.py"],
                                "focus": ["Check for lookahead bias"],
                            },
                            {
                                "id": "engine",
                                "title": "Execution engine",
                                "paths": ["src/engine/runner.py"],
                                "focus": ["Check error handling"],
                            },
                        ],
                    }
                    return 0, json.dumps(plan), plan
                synth = {
                    "executive_summary": "Found 1 high-severity lookahead issue in factors.",
                    "systemic_risks": ["Missing prefix-invariance tests"],
                    "recommended_gates": ["Add prefix-invariance test suite to factory.toml"],
                }
                return 0, json.dumps(synth), synth

            def reviewer_handler(inv: AgentInvocation, _call: int) -> tuple[int, str, dict[str, Any] | None]:
                self.assertTrue(inv.read_only)
                if "Shard `factors`" in inv.prompt:
                    payload = {
                        "shard_summary": "Lookahead risk detected.",
                        "findings": [
                            {
                                "severity": "high",
                                "category": "lookahead-bias",
                                "file": "src/factors/mom.py",
                                "line": 2,
                                "title": "Missing rolling window boundary",
                                "description": "Uses full series instead of trailing window.",
                                "evidence": "return xs",
                                "fix_hint": "Use trailing window only.",
                                "ratchet_rule_idea": "Add prefix-invariance test",
                            }
                        ],
                    }
                else:
                    payload = {
                        "shard_summary": "Clean shard.",
                        "findings": [
                            {
                                "severity": "low",
                                "category": "observability",
                                "file": "src/engine/runner.py",
                                "line": 1,
                                "title": "No return type annotation",
                                "description": "Add explicit return type.",
                                "evidence": "def run():",
                                "fix_hint": "Annotate -> int.",
                                "ratchet_rule_idea": "Run mypy in gates",
                            }
                        ],
                    }
                return 0, json.dumps(payload), payload

            agy_mock = MockAdapter("agy", handler=lead_handler)
            codex_mock = MockAdapter("codex", handler=reviewer_handler)
            reg = AdapterRegistry(
                adapters={"agy": agy_mock, "codex": codex_mock},
                routing={"planner": ["agy"], "worker": ["codex"]},
            )
            auditor = RepoAuditor(repo_root=repo, config=FactoryConfig(), registry=reg)
            result = auditor.run_audit(
                lead_adapter="agy",
                reviewer_adapter="codex",
                max_workers=2,
                focus="quant lookahead bias",
                audit_id="test-audit-01",
            )

            self.assertEqual(result.lead_adapter, "agy")
            self.assertEqual(result.reviewer_adapter, "codex")
            self.assertEqual(len(result.shard_errors), 0)
            # 2 planned shards + 1 auto-covered remaining Python file (tests/test_mom.py)
            self.assertEqual(len(result.plan.shards), 3)
            # Lead called twice: 1 plan + 1 synthesis
            self.assertEqual(agy_mock.call_count, 2)
            # Reviewer called once per shard
            self.assertEqual(codex_mock.call_count, 3)
            # Sorted by severity: high before low
            self.assertEqual(result.findings[0].severity, "high")
            self.assertIn("prefix-invariance", result.recommended_gates[0])
            self.assertTrue(Path(result.report_path).is_file())
            self.assertTrue(Path(result.data_path).is_file())
            report_text = Path(result.report_path).read_text(encoding="utf-8")
            self.assertIn("Missing rolling window boundary", report_text)


if __name__ == "__main__":
    unittest.main()
