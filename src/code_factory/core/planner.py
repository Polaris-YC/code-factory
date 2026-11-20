"""Task DAG validation, topological wave computation, and scope overlap checks."""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from fnmatch import fnmatch

from code_factory.core.models import TaskDAG, TaskSpec


class DAGValidationError(ValueError):
    """Raised when a TaskDAG violates structural or ownership invariants."""


@dataclass(frozen=True)
class ExecutionPlan:
    dag: TaskDAG
    waves: list[list[TaskSpec]]


def _pattern_prefix(pattern: str) -> str:
    clean = pattern.strip().lstrip("./")
    for idx, ch in enumerate(clean):
        if ch in "*?[":
            return clean[:idx].rstrip("/")
    return clean.rstrip("/")


def paths_may_overlap(patterns_a: list[str], patterns_b: list[str]) -> bool:
    """Conservative static overlap check between two task ownership glob sets."""
    if not patterns_a or not patterns_b:
        return True
    for a in patterns_a:
        na = a.strip().lstrip("./")
        pa = _pattern_prefix(na)
        for b in patterns_b:
            nb = b.strip().lstrip("./")
            pb = _pattern_prefix(nb)
            if na == nb or fnmatch(na, nb) or fnmatch(nb, na):
                return True
            if not pa or not pb:
                return True
            if pa == pb or pa.startswith(pb + "/") or pb.startswith(pa + "/"):
                return True
    return False


def validate_and_plan(
    dag: TaskDAG,
    allow_parallel_overlap: bool = False,
) -> ExecutionPlan:
    """Validate TaskDAG acyclicity, dependencies, and parallel scope isolation."""
    if not dag.tasks:
        raise DAGValidationError("TaskDAG must contain at least one task.")

    by_id: dict[str, TaskSpec] = {}
    for task in dag.tasks:
        if not task.id.strip():
            raise DAGValidationError("Task id must be non-empty.")
        if task.id in by_id:
            raise DAGValidationError(f"Duplicate task id: '{task.id}'")
        by_id[task.id] = task

    indegree: dict[str, int] = {tid: 0 for tid in by_id}
    dependents: dict[str, list[str]] = {tid: [] for tid in by_id}

    for task in dag.tasks:
        for dep in task.depends_on:
            if dep == task.id:
                raise DAGValidationError(f"Task '{task.id}' cannot depend on itself.")
            if dep not in by_id:
                raise DAGValidationError(
                    f"Task '{task.id}' depends on unknown task '{dep}'."
                )
            dependents[dep].append(task.id)
            indegree[task.id] += 1

    ready = deque(sorted(tid for tid, deg in indegree.items() if deg == 0))
    waves: list[list[TaskSpec]] = []
    visited = 0

    while ready:
        wave_ids = list(ready)
        ready.clear()
        wave_tasks = [by_id[tid] for tid in wave_ids]

        if not allow_parallel_overlap and len(wave_tasks) > 1:
            for idx, left in enumerate(wave_tasks):
                for right in wave_tasks[idx + 1 :]:
                    if paths_may_overlap(left.owned_paths, right.owned_paths):
                        raise DAGValidationError(
                            f"Parallel tasks '{left.id}' and '{right.id}' have "
                            f"overlapping owned_paths ({left.owned_paths} vs "
                            f"{right.owned_paths}). Add a dependency or narrow owned_paths."
                        )

        waves.append(wave_tasks)
        visited += len(wave_ids)

        next_ready: list[str] = []
        for tid in wave_ids:
            for child in dependents[tid]:
                indegree[child] -= 1
                if indegree[child] == 0:
                    next_ready.append(child)
        for tid in sorted(next_ready):
            ready.append(tid)

    if visited != len(by_id):
        raise DAGValidationError("TaskDAG contains a dependency cycle.")

    return ExecutionPlan(dag=dag, waves=waves)
