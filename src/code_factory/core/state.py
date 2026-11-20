"""Thread-safe JSON run state and artifact storage."""

from __future__ import annotations

import json
import threading
from pathlib import Path
from typing import Any

from code_factory.core.models import (
    GateReport,
    ReviewVerdict,
    RunState,
    TaskDAG,
    utc_now_iso,
)


class RunStore:
    def __init__(self, repo_root: Path, run_id: str) -> None:
        self.repo_root = repo_root.resolve()
        self.run_id = run_id
        self.run_dir = self.repo_root / ".code-factory" / "runs" / run_id
        self.run_dir.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()

    @property
    def state_path(self) -> Path:
        return self.run_dir / "state.json"

    @property
    def dag_path(self) -> Path:
        return self.run_dir / "dag.json"

    def task_dir(self, task_id: str) -> Path:
        safe = task_id.replace("/", "-")
        path = self.run_dir / "tasks" / safe
        path.mkdir(parents=True, exist_ok=True)
        return path

    def save_dag(self, dag: TaskDAG) -> None:
        with self._lock:
            self.dag_path.write_text(
                json.dumps(dag.to_dict(), indent=2),
                encoding="utf-8",
            )

    def load_dag(self) -> TaskDAG:
        data = json.loads(self.dag_path.read_text(encoding="utf-8"))
        return TaskDAG.from_dict(data)

    def save_state(self, state: RunState) -> None:
        with self._lock:
            state.updated_at = utc_now_iso()
            tmp = self.state_path.with_suffix(".json.tmp")
            tmp.write_text(
                json.dumps(state.to_dict(), indent=2),
                encoding="utf-8",
            )
            tmp.replace(self.state_path)

    def load_state(self) -> RunState:
        data = json.loads(self.state_path.read_text(encoding="utf-8"))
        return RunState.from_dict(data)

    def save_gate_report(self, task_id: str, attempt: int, report: GateReport) -> Path:
        out = self.task_dir(task_id) / f"gate_attempt_{attempt}.json"
        out.write_text(json.dumps(report.to_dict(), indent=2), encoding="utf-8")
        return out

    def save_review_verdict(self, task_id: str, round_num: int, verdict: ReviewVerdict) -> Path:
        out = self.task_dir(task_id) / f"review_round_{round_num}.json"
        out.write_text(json.dumps(verdict.to_dict(), indent=2), encoding="utf-8")
        return out

    def write_markdown(self, filename: str, content: str) -> Path:
        out = self.run_dir / filename
        out.write_text(content, encoding="utf-8")
        return out

    @staticmethod
    def list_runs(repo_root: Path) -> list[dict[str, Any]]:
        runs_root = repo_root.resolve() / ".code-factory" / "runs"
        if not runs_root.is_dir():
            return []
        entries: list[dict[str, Any]] = []
        for child in sorted(runs_root.iterdir(), reverse=True):
            state_file = child / "state.json"
            if state_file.is_file():
                try:
                    entries.append(json.loads(state_file.read_text(encoding="utf-8")))
                except (OSError, json.JSONDecodeError):
                    continue
        return entries
