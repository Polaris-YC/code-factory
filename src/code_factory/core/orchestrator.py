"""Autonomous parallel orchestrator implementing the poteto inner loop."""

from __future__ import annotations

import json
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any

from code_factory.adapters.registry import AdapterRegistry, AdapterResolutionError
from code_factory.config import FactoryConfig
from code_factory.core.models import (
    AgentInvocation,
    AgentRole,
    FactoryMode,
    GateReport,
    ReviewDecision,
    ReviewIssue,
    ReviewVerdict,
    RunState,
    TaskDAG,
    TaskExecutionState,
    TaskSpec,
    TaskStatus,
)
from code_factory.core.planner import validate_and_plan
from code_factory.core.ratchet import RatchetEntry, RatchetInbox
from code_factory.core.state import RunStore
from code_factory.core.worktree import GitWorktreeManager, MergeConflictError
from code_factory.gates.runner import GateRunner

REVIEW_VERDICT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "required": ["decision", "summary", "issues"],
    "properties": {
        "decision": {"type": "string", "enum": ["APPROVE", "REQUEST_CHANGES"]},
        "summary": {"type": "string"},
        "issues": {
            "type": "array",
            "items": {
                "type": "object",
                "required": ["severity", "file", "rule", "message"],
                "properties": {
                    "severity": {"type": "string", "enum": ["high", "medium", "low"]},
                    "file": {"type": "string"},
                    "line": {"type": ["integer", "null"]},
                    "rule": {"type": "string"},
                    "message": {"type": "string"},
                    "fix_hint": {"type": "string"},
                },
            },
        },
    },
}

PLAN_DAG_SCHEMA: dict[str, Any] = {
    "type": "object",
    "required": ["goal", "tasks"],
    "properties": {
        "goal": {"type": "string"},
        "tasks": {
            "type": "array",
            "minItems": 1,
            "items": {
                "type": "object",
                "required": ["id", "title", "description", "owned_paths"],
                "properties": {
                    "id": {"type": "string"},
                    "title": {"type": "string"},
                    "description": {"type": "string"},
                    "depends_on": {"type": "array", "items": {"type": "string"}},
                    "owned_paths": {"type": "array", "items": {"type": "string"}},
                    "done_checks": {"type": "array", "items": {"type": "string"}},
                    "preferred_adapter": {"type": ["string", "null"]},
                },
            },
        },
    },
}


def _build_worker_prompt(
    dag: TaskDAG,
    task: TaskSpec,
    *,
    feedback: str | None = None,
) -> str:
    owned = ", ".join(task.owned_paths) if task.owned_paths else "(any repo file)"
    checks = (
        "\n".join(f"- `{c}`" for c in task.done_checks)
        if task.done_checks
        else "- Repository deterministic gates"
    )
    sections = [
        f"# Code Factory Task: {task.id} — {task.title}",
        f"Overall Goal: {dag.goal}",
        "",
        "## Instructions",
        task.description.strip() or task.title,
        "",
        "## Strict Guardrails (Inverse Trust Pyramid)",
        f"- Allowed file scope (`owned_paths`): {owned}",
        "- Do NOT edit files outside your allowed scope.",
        "- Prefer minimal, clean, idiomatic code (deletion bias; no speculative abstractions).",
        "- Do NOT leave placeholder TODOs, stubs, or explanatory comment bloat.",
        "- Verify your work locally before exiting.",
        "",
        "## Falsifiable Done Checks",
        checks,
    ]
    if feedback:
        sections.extend(
            [
                "",
                "## Automated Diagnostics / Review Feedback to Fix",
                "Fix every issue listed below without introducing regressions:",
                feedback,
            ]
        )
    return "\n".join(sections) + "\n"


def _build_reviewer_prompt(
    dag: TaskDAG,
    task: TaskSpec,
    diff_text: str,
    gate_report: GateReport,
) -> str:
    gate_lines = [
        f"- {c.name}: {'PASS' if c.passed else 'FAIL'} ({c.summary})"
        for c in gate_report.checks
    ]
    truncated_diff = diff_text
    if len(truncated_diff) > 24000:
        truncated_diff = truncated_diff[:24000] + "\n... [diff truncated]"

    return "\n".join(
        [
            f"# Adversarial Anti-Slop Review: {task.id} — {task.title}",
            f"Overall Goal: {dag.goal}",
            "",
            "## Task Specification",
            task.description.strip() or task.title,
            f"Allowed paths: {task.owned_paths}",
            "",
            "## Deterministic Gate Evidence",
            *gate_lines,
            "",
            "## Review Rubric",
            "1. Correctness & edge cases: Does the diff satisfy THIS task's specification? Judge ONLY against this task's spec and `owned_paths`; other parts of the overall DAG goal belong to separate tasks.",
            "2. Deletion bias: Reject dead code, redundant wrappers, or over-engineering.",
            "3. Scope discipline: Reject unrelated edits or style churn.",
            "4. Anti-slop: Reject stale/excuse comments, silent error swallowing, or fake tests.",
            "5. Human-readable reporting: On first mention of any repo-internal file, class, or function in `summary`, `message`, or `fix_hint`, add a brief parenthetical role gloss (3-10 words) so a human reader does not need to memorize internal symbols; skip stdlib/common terms and do not repeat glosses.",
            "",
            "Respond ONLY with a valid JSON object matching the ReviewVerdict schema:",
            '{"decision": "APPROVE" | "REQUEST_CHANGES", "summary": "...", "issues": [...]}',
            "",
            "## Diff",
            "```diff",
            truncated_diff,
            "```",
        ]
    )


def _parse_review_verdict(
    parsed: dict[str, Any] | None,
    raw_message: str,
    reviewer_name: str,
) -> ReviewVerdict:
    if parsed and parsed.get("decision") in (
        ReviewDecision.APPROVE.value,
        ReviewDecision.REQUEST_CHANGES.value,
    ):
        issues = [
            ReviewIssue.from_dict(item)
            for item in parsed.get("issues", [])
            if isinstance(item, dict)
        ]
        return ReviewVerdict(
            decision=ReviewDecision(parsed["decision"]),
            summary=str(parsed.get("summary", "")),
            reviewer_adapter=reviewer_name,
            issues=issues,
        )

    return ReviewVerdict(
        decision=ReviewDecision.REQUEST_CHANGES,
        summary="Reviewer returned malformed or non-JSON verdict.",
        reviewer_adapter=reviewer_name,
        issues=[
            ReviewIssue(
                severity="high",
                file="<review>",
                line=None,
                rule="reviewer-protocol",
                message=f"Expected ReviewVerdict JSON; got: {raw_message[:200]}",
                fix_hint="Re-verify task diff and rerun review.",
            )
        ],
    )


class FactoryOrchestrator:
    def __init__(
        self,
        *,
        repo_root: Path,
        config: FactoryConfig,
        registry: AdapterRegistry,
        gate_runner: GateRunner | None = None,
        worktree_manager: GitWorktreeManager | None = None,
    ) -> None:
        self.repo_root = repo_root.resolve()
        self.config = config
        self.registry = registry
        self.gate_runner = gate_runner or GateRunner(config)
        self.worktree_manager = worktree_manager or GitWorktreeManager(
            self.repo_root, worktree_root=config.worktree_root
        )
        self.ratchet = RatchetInbox(self.repo_root)

    def synthesize_dag(
        self,
        *,
        goal: str,
        base_branch: str | None = None,
        mode: FactoryMode | None = None,
        planner_adapter: str | None = None,
    ) -> TaskDAG:
        """Invoke the configured planner CLI to convert a goal into a validated TaskDAG."""
        run_id = f"run-{uuid.uuid4().hex[:8]}"
        branch = base_branch or self.worktree_manager.current_branch()
        adapter = self.registry.pick_adapter("planner", preferred=planner_adapter)
        store = RunStore(self.repo_root, run_id)

        prompt = "\n".join(
            [
                "# Code Factory Planner",
                f"Repository: {self.repo_root}",
                f"Goal: {goal}",
                "",
                "Decompose this goal into a small, crisp Task DAG.",
                "Rules:",
                "- Tasks that can run in parallel MUST have disjoint `owned_paths`.",
                "- Use `depends_on` whenever one task builds on another.",
                "- Every task should have concrete `done_checks` shell commands when possible.",
                "Return ONLY valid JSON matching the TaskDAG schema.",
            ]
        )
        result = adapter.run(
            AgentInvocation(
                prompt=prompt,
                cwd=str(self.repo_root),
                role=AgentRole.PLANNER,
                transcript_path=str(store.run_dir / "planner_transcript.log"),
                timeout_seconds=self.config.task_timeout_seconds,
                read_only=True,
                json_schema=PLAN_DAG_SCHEMA,
            )
        )
        if not result.ok or not result.parsed_json:
            raise RuntimeError(
                f"Planner adapter '{adapter.name}' failed to emit a valid TaskDAG JSON "
                f"(exit={result.exit_code}). See {result.transcript_path}"
            )
        payload = dict(result.parsed_json)
        payload["run_id"] = run_id
        payload["base_branch"] = branch
        payload["mode"] = (mode or self.config.mode).value
        dag = TaskDAG.from_dict(payload)
        validate_and_plan(dag, permissions=self.config.permissions)
        return dag

    def run_dag(
        self,
        dag: TaskDAG,
        *,
        create_pr: bool = False,
        keep_worktrees: bool = False,
        cross_model_review: bool | None = None,
        resume_state: RunState | None = None,
    ) -> RunState:
        """Execute (or resume) a validated TaskDAG end-to-end with bounded self-healing."""
        plan = validate_and_plan(dag, permissions=self.config.permissions)
        store = RunStore(self.repo_root, dag.run_id)
        store.save_dag(dag)

        use_cross_model = (
            self.config.cross_model_review
            if cross_model_review is None
            else cross_model_review
        )

        if resume_state is not None:
            state = resume_state
            for t in dag.tasks:
                if t.id not in state.tasks:
                    state.tasks[t.id] = TaskExecutionState(task_id=t.id)
        else:
            state = RunState(
                run_id=dag.run_id,
                goal=dag.goal,
                repo_root=str(self.repo_root),
                base_branch=dag.base_branch,
                mode=dag.mode,
                tasks={t.id: TaskExecutionState(task_id=t.id) for t in dag.tasks},
            )
        state.integrated = False
        store.save_state(state)

        integration = self.worktree_manager.create_integration_worktree(
            dag.run_id, dag.base_branch
        )
        state.integration_branch = integration.branch
        store.save_state(state)

        wave_base_ref = integration.branch
        all_passed = True

        for wave in plan.waves:
            wave_results: dict[str, TaskExecutionState] = {}
            to_run: list[TaskSpec] = []
            for task in wave:
                existing = state.tasks[task.id]
                if (
                    existing.status == TaskStatus.PASSED
                    and existing.branch
                    and self.worktree_manager.ensure_branch_exists(existing.branch)
                ):
                    wave_results[task.id] = existing
                else:
                    existing.status = TaskStatus.PENDING
                    existing.escalation_reason = None
                    to_run.append(task)

            if to_run:
                max_workers = min(self.config.max_workers, len(to_run))
                with ThreadPoolExecutor(max_workers=max_workers) as pool:
                    futures = {
                        pool.submit(
                            self._execute_task,
                            dag=dag,
                            task=task,
                            base_ref=wave_base_ref,
                            store=store,
                            state=state,
                            keep_worktrees=keep_worktrees,
                            cross_model_review=use_cross_model,
                        ): task.id
                        for task in to_run
                    }
                    for fut in as_completed(futures):
                        tid = futures[fut]
                        wave_results[tid] = fut.result()

            for task in wave:
                t_state = wave_results[task.id]
                if t_state.status != TaskStatus.PASSED or not t_state.branch:
                    all_passed = False
                    continue
                try:
                    self.worktree_manager.merge_branch(
                        integration.path,
                        t_state.branch,
                        message=f"code-factory({dag.run_id}): integrate {task.id}",
                    )
                except MergeConflictError as exc:
                    t_state.status = TaskStatus.ESCALATED
                    t_state.escalation_reason = str(exc)
                    store.save_state(state)
                    self.ratchet.record(
                        RatchetEntry(
                            run_id=dag.run_id,
                            task_id=task.id,
                            source="integration",
                            rule="merge-conflict",
                            summary=str(exc),
                            files=exc.files,
                        )
                    )
                    all_passed = False

            if not all_passed:
                break
            wave_base_ref = integration.branch

        integrated_gate_report: GateReport | None = None
        if all_passed:
            int_files = self.worktree_manager.changed_files(
                integration.path, dag.base_branch
            )
            int_diff = self.worktree_manager.unified_diff(
                integration.path, dag.base_branch
            )
            integrated_gate_report = self.gate_runner.run_all(
                cwd=integration.path,
                changed_files=int_files,
                diff_text=int_diff,
                owned_paths=None,
                done_checks=[],
                log_dir=store.run_dir / "integration_gates",
            )
            store.write_markdown(
                "integration_gate.json",
                json.dumps(integrated_gate_report.to_dict(), indent=2),
            )
            if integrated_gate_report.passed:
                state.integrated = True
            else:
                all_passed = False
                for failed in integrated_gate_report.failed_checks():
                    self.ratchet.record(
                        RatchetEntry(
                            run_id=dag.run_id,
                            task_id="_integrate",
                            source="gate",
                            rule=failed.name,
                            summary=failed.summary,
                            excerpt=failed.excerpt,
                        )
                    )

        if not keep_worktrees and state.integrated:
            self.worktree_manager.remove_worktree(integration.path)

        if state.integrated and dag.mode == FactoryMode.DARK:
            self.worktree_manager.merge_into_base(integration.branch, dag.base_branch)
            state.merged_to_base = True

        report_md = self._render_report(state, integrated_gate_report)
        store.write_markdown("report.md", report_md)

        if not state.integrated:
            escalation_md = self._render_escalation(state, integrated_gate_report)
            store.write_markdown("escalation.md", escalation_md)
        elif create_pr and not state.merged_to_base:
            state.pr_url = self.worktree_manager.create_pr(
                branch=integration.branch,
                base_branch=dag.base_branch,
                title=f"[Code Factory] {dag.goal[:72]}",
                body=report_md,
            )

        store.save_state(state)
        return state

    def resume_run(
        self,
        run_id: str,
        *,
        create_pr: bool = False,
        keep_worktrees: bool = False,
        cross_model_review: bool | None = None,
    ) -> RunState:
        """Resume an interrupted or escalated run, reusing already-PASSED task branches."""
        store = RunStore(self.repo_root, run_id)
        if not store.dag_path.is_file() or not store.state_path.is_file():
            raise FileNotFoundError(f"Run `{run_id}` not found in {store.run_dir}")
        dag = store.load_dag()
        prior_state = store.load_state()
        return self.run_dag(
            dag,
            create_pr=create_pr,
            keep_worktrees=keep_worktrees,
            cross_model_review=cross_model_review,
            resume_state=prior_state,
        )

    def _execute_task(
        self,
        *,
        dag: TaskDAG,
        task: TaskSpec,
        base_ref: str,
        store: RunStore,
        state: RunState,
        keep_worktrees: bool,
        cross_model_review: bool = True,
    ) -> TaskExecutionState:
        t_state = state.tasks[task.id]
        t_dir = store.task_dir(task.id)

        worker = self.registry.pick_adapter("worker", preferred=task.preferred_adapter)
        t_state.worker_adapter = worker.name

        wt = self.worktree_manager.create_worktree(dag.run_id, task.id, base_ref)
        t_state.branch = wt.branch
        t_state.worktree_path = str(wt.path)
        t_state.status = TaskStatus.RUNNING
        store.save_state(state)

        feedback: str | None = None
        max_gate_attempts = 1 + self.config.max_gate_retries
        max_review_rounds = self.config.max_review_rounds

        for review_round in range(max_review_rounds + 1):
            gate_report: GateReport | None = None
            gate_passed = False

            for attempt in range(1, max_gate_attempts + 1):
                t_state.status = TaskStatus.RUNNING
                t_state.gate_attempts += 1
                store.save_state(state)

                worker_prompt = _build_worker_prompt(dag, task, feedback=feedback)
                worker_res = worker.run(
                    AgentInvocation(
                        prompt=worker_prompt,
                        cwd=str(wt.path),
                        role=AgentRole.WORKER,
                        transcript_path=str(
                            t_dir / f"worker_r{review_round}_a{attempt}.log"
                        ),
                        timeout_seconds=self.config.task_timeout_seconds,
                        read_only=False,
                    )
                )

                t_state.status = TaskStatus.GATING
                store.save_state(state)

                changed = self.worktree_manager.changed_files(wt.path, base_ref)
                diff_text = self.worktree_manager.unified_diff(wt.path, base_ref)
                gate_report = self.gate_runner.run_all(
                    cwd=wt.path,
                    changed_files=changed,
                    diff_text=diff_text,
                    owned_paths=task.owned_paths,
                    done_checks=task.done_checks,
                    max_diff_lines_override=task.max_diff_lines,
                    log_dir=t_dir / f"gates_r{review_round}_a{attempt}",
                )
                store.save_gate_report(task.id, t_state.gate_attempts, gate_report)
                t_state.last_gate_passed = gate_report.passed

                if worker_res.ok and gate_report.passed:
                    gate_passed = True
                    break

                diag_parts: list[str] = []
                if not worker_res.ok:
                    diag_parts.append(
                        f"Worker CLI `{worker.name}` exited with code {worker_res.exit_code} "
                        f"(timed_out={worker_res.timed_out})."
                    )
                if not gate_report.passed:
                    diag_parts.append(gate_report.format_diagnostics())
                    for failed in gate_report.failed_checks():
                        self.ratchet.record(
                            RatchetEntry(
                                run_id=dag.run_id,
                                task_id=task.id,
                                source="gate",
                                rule=failed.name,
                                summary=failed.summary,
                                excerpt=failed.excerpt,
                                files=changed,
                            )
                        )
                feedback = "\n\n".join(diag_parts)

            if not gate_passed or gate_report is None:
                t_state.status = TaskStatus.ESCALATED
                t_state.escalation_reason = (
                    f"Exceeded max gate retries ({self.config.max_gate_retries}). "
                    f"Latest diagnostics:\n{feedback or 'unknown failure'}"
                )
                store.save_state(state)
                return t_state

            t_state.commit_sha = self.worktree_manager.commit_all(
                wt.path,
                message=f"code-factory({dag.run_id}/{task.id}): {task.title}",
            )

            if max_review_rounds == 0:
                t_state.status = TaskStatus.PASSED
                store.save_state(state)
                if not keep_worktrees:
                    self.worktree_manager.remove_worktree(wt.path)
                return t_state

            t_state.status = TaskStatus.REVIEWING
            t_state.review_rounds += 1
            exclude_reviewers = (worker.name,) if cross_model_review else ()
            reviewer = self.registry.pick_adapter(
                "reviewer",
                preferred=task.reviewer_adapter,
                exclude=exclude_reviewers,
                allow_fallback_to_excluded=True,
            )
            t_state.reviewer_adapter = reviewer.name
            store.save_state(state)

            review_diff = self.worktree_manager.unified_diff(wt.path, base_ref)
            review_prompt = _build_reviewer_prompt(dag, task, review_diff, gate_report)
            review_res = reviewer.run(
                AgentInvocation(
                    prompt=review_prompt,
                    cwd=str(wt.path),
                    role=AgentRole.REVIEWER,
                    transcript_path=str(t_dir / f"reviewer_r{t_state.review_rounds}.log"),
                    timeout_seconds=self.config.reviewer_timeout_seconds,
                    read_only=True,
                    json_schema=REVIEW_VERDICT_SCHEMA,
                )
            )
            # If the reviewer CLI itself timed out or crashed, try one fallback reviewer
            # before escalating; never send an already-passing worker back to re-edit code.
            if not review_res.ok:
                fallback = None
                try:
                    fallback = self.registry.pick_adapter(
                        "reviewer",
                        preferred=None,
                        exclude=(reviewer.name,),
                        allow_fallback_to_excluded=False,
                    )
                except AdapterResolutionError:
                    fallback = None
                if fallback is not None and fallback.name != reviewer.name:
                    reviewer = fallback
                    t_state.reviewer_adapter = reviewer.name
                    store.save_state(state)
                    review_res = reviewer.run(
                        AgentInvocation(
                            prompt=review_prompt,
                            cwd=str(wt.path),
                            role=AgentRole.REVIEWER,
                            transcript_path=str(
                                t_dir / f"reviewer_r{t_state.review_rounds}_fallback.log"
                            ),
                            timeout_seconds=self.config.reviewer_timeout_seconds,
                            read_only=True,
                            json_schema=REVIEW_VERDICT_SCHEMA,
                        )
                    )

            if not review_res.ok:
                t_state.status = TaskStatus.ESCALATED
                t_state.escalation_reason = (
                    f"Reviewer CLI `{reviewer.name}` infrastructure failure "
                    f"(exit={review_res.exit_code}, timed_out={review_res.timed_out}). "
                    f"Passing worker commit `{t_state.commit_sha}` preserved on `{t_state.branch}`; "
                    f"fix reviewer config or rerun with `--no-cross-model` and `cf resume {dag.run_id}`."
                )
                self.ratchet.record(
                    RatchetEntry(
                        run_id=dag.run_id,
                        task_id=task.id,
                        source="review",
                        rule="reviewer-infra-failure",
                        summary=t_state.escalation_reason,
                    )
                )
                store.save_state(state)
                return t_state

            verdict = _parse_review_verdict(
                review_res.parsed_json, review_res.final_message, reviewer.name
            )
            store.save_review_verdict(task.id, t_state.review_rounds, verdict)
            t_state.last_review_decision = verdict.decision.value

            if verdict.approved:
                t_state.status = TaskStatus.PASSED
                store.save_state(state)
                if not keep_worktrees:
                    self.worktree_manager.remove_worktree(wt.path)
                return t_state

            for issue in verdict.issues:
                self.ratchet.record(
                    RatchetEntry(
                        run_id=dag.run_id,
                        task_id=task.id,
                        source="review",
                        rule=issue.rule,
                        summary=issue.message,
                        excerpt=issue.fix_hint,
                        files=[issue.file] if issue.file else [],
                    )
                )

            if review_round >= max_review_rounds:
                t_state.status = TaskStatus.ESCALATED
                t_state.escalation_reason = (
                    f"Reviewer `{reviewer.name}` requested changes after "
                    f"{t_state.review_rounds} round(s):\n{verdict.format_feedback()}"
                )
                store.save_state(state)
                return t_state

            feedback = verdict.format_feedback()

        t_state.status = TaskStatus.ESCALATED
        t_state.escalation_reason = "Task exited loop without approval."
        store.save_state(state)
        return t_state

    def _render_report(
        self,
        state: RunState,
        integrated_gate: GateReport | None,
    ) -> str:
        lines = [
            f"# Code Factory Run Report: `{state.run_id}`",
            "",
            f"- **Goal**: {state.goal}",
            f"- **Mode**: `{state.mode.value}`",
            f"- **Base branch**: `{state.base_branch}`",
            f"- **Integration branch**: `{state.integration_branch}`",
            f"- **Integrated**: `{state.integrated}`",
            f"- **Merged to base**: `{state.merged_to_base}`",
        ]
        if state.pr_url:
            lines.append(f"- **PR**: {state.pr_url}")
        lines.extend(
            [
                "",
                "## Tasks",
                "",
                "| Task | Status | Worker | Reviewer | Gate Attempts | Review Rounds | Commit |",
                "| :--- | :--- | :--- | :--- | ---: | ---: | :--- |",
            ]
        )
        for tid, ts in state.tasks.items():
            sha = (ts.commit_sha or "-")[:8]
            lines.append(
                f"| `{tid}` | `{ts.status.value}` | `{ts.worker_adapter or '-'}` | "
                f"`{ts.reviewer_adapter or '-'}` | {ts.gate_attempts} | {ts.review_rounds} | `{sha}` |"
            )

        if integrated_gate is not None:
            lines.extend(["", "## Integrated Full-Repo Gates", ""])
            for check in integrated_gate.checks:
                mark = "PASS" if check.passed else "FAIL"
                lines.append(f"- **{check.name}**: `{mark}` — {check.summary}")
        return "\n".join(lines) + "\n"

    def _render_escalation(
        self,
        state: RunState,
        integrated_gate: GateReport | None,
    ) -> str:
        lines = [
            f"# Escalation Required: `{state.run_id}`",
            "",
            "The factory paused because one or more tasks or integration gates exhausted bounded autonomous retries.",
            "",
        ]
        for tid, ts in state.tasks.items():
            if ts.status != TaskStatus.PASSED:
                lines.extend(
                    [
                        f"## Task `{tid}` ({ts.status.value})",
                        f"- Branch: `{ts.branch}`",
                        f"- Worktree: `{ts.worktree_path}`",
                        f"- Reason:\n\n{ts.escalation_reason or 'Not executed due to upstream failure.'}",
                        "",
                    ]
                )
        if integrated_gate is not None and not integrated_gate.passed:
            lines.extend(
                [
                    "## Integrated Gate Failures",
                    integrated_gate.format_diagnostics(),
                    "",
                ]
            )
        return "\n".join(lines) + "\n"
