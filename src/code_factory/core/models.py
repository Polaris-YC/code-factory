"""Core immutable and serializable domain models for Code Factory."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from enum import StrEnum
from typing import Any


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class FactoryMode(StrEnum):
    SOFTWARE = "software"
    DARK = "dark"


class AgentRole(StrEnum):
    PLANNER = "planner"
    WORKER = "worker"
    REVIEWER = "reviewer"
    HARDENER = "hardener"


class TaskStatus(StrEnum):
    PENDING = "PENDING"
    RUNNING = "RUNNING"
    GATING = "GATING"
    REVIEWING = "REVIEWING"
    PASSED = "PASSED"
    ESCALATED = "ESCALATED"
    FAILED = "FAILED"


class ReviewDecision(StrEnum):
    APPROVE = "APPROVE"
    REQUEST_CHANGES = "REQUEST_CHANGES"


@dataclass(frozen=True)
class TaskSpec:
    id: str
    title: str
    description: str
    depends_on: list[str] = field(default_factory=list)
    owned_paths: list[str] = field(default_factory=list)
    done_checks: list[str] = field(default_factory=list)
    preferred_adapter: str | None = None
    reviewer_adapter: str | None = None
    max_diff_lines: int | None = None

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> TaskSpec:
        return cls(
            id=str(data["id"]),
            title=str(data["title"]),
            description=str(data.get("description", "")),
            depends_on=[str(x) for x in data.get("depends_on", [])],
            owned_paths=[str(x) for x in data.get("owned_paths", [])],
            done_checks=[str(x) for x in data.get("done_checks", [])],
            preferred_adapter=data.get("preferred_adapter"),
            reviewer_adapter=data.get("reviewer_adapter"),
            max_diff_lines=data.get("max_diff_lines"),
        )


@dataclass(frozen=True)
class TaskDAG:
    run_id: str
    goal: str
    base_branch: str
    tasks: list[TaskSpec]
    mode: FactoryMode = FactoryMode.SOFTWARE

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["mode"] = self.mode.value
        return payload

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> TaskDAG:
        mode_raw = str(data.get("mode", FactoryMode.SOFTWARE.value))
        return cls(
            run_id=str(data["run_id"]),
            goal=str(data["goal"]),
            base_branch=str(data.get("base_branch", "main")),
            mode=FactoryMode(mode_raw),
            tasks=[TaskSpec.from_dict(item) for item in data.get("tasks", [])],
        )


@dataclass(frozen=True)
class AgentInvocation:
    prompt: str
    cwd: str
    role: AgentRole
    transcript_path: str
    timeout_seconds: int = 600
    read_only: bool = False
    json_schema: dict[str, Any] | None = None
    extra_env: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class AgentResult:
    adapter: str
    exit_code: int
    stdout: str
    stderr: str
    final_message: str
    duration_seconds: float
    transcript_path: str
    timed_out: bool = False
    parsed_json: dict[str, Any] | None = None

    @property
    def ok(self) -> bool:
        return self.exit_code == 0 and not self.timed_out


@dataclass(frozen=True)
class GateCheckResult:
    name: str
    passed: bool
    exit_code: int
    summary: str
    excerpt: str = ""
    duration_seconds: float = 0.0
    log_path: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> GateCheckResult:
        return cls(**data)


@dataclass(frozen=True)
class GateReport:
    passed: bool
    checks: list[GateCheckResult]
    created_at: str = field(default_factory=utc_now_iso)

    def failed_checks(self) -> list[GateCheckResult]:
        return [c for c in self.checks if not c.passed]

    def format_diagnostics(self, max_chars: int = 4000) -> str:
        lines: list[str] = []
        for check in self.failed_checks():
            lines.append(f"### Gate FAILED: {check.name} (exit={check.exit_code})")
            lines.append(check.summary)
            if check.excerpt:
                lines.append("```text")
                lines.append(check.excerpt.strip())
                lines.append("```")
        text = "\n".join(lines)
        if len(text) > max_chars:
            return text[: max_chars - 30] + "\n... [diagnostics truncated]"
        return text

    def to_dict(self) -> dict[str, Any]:
        return {
            "passed": self.passed,
            "created_at": self.created_at,
            "checks": [c.to_dict() for c in self.checks],
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> GateReport:
        return cls(
            passed=bool(data["passed"]),
            created_at=str(data.get("created_at", utc_now_iso())),
            checks=[GateCheckResult.from_dict(c) for c in data.get("checks", [])],
        )


@dataclass(frozen=True)
class ReviewIssue:
    severity: str
    file: str
    line: int | None
    rule: str
    message: str
    fix_hint: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ReviewIssue:
        return cls(
            severity=str(data.get("severity", "medium")),
            file=str(data.get("file", "")),
            line=data.get("line"),
            rule=str(data.get("rule", "anti-slop")),
            message=str(data.get("message", "")),
            fix_hint=str(data.get("fix_hint", "")),
        )


@dataclass(frozen=True)
class ReviewVerdict:
    decision: ReviewDecision
    summary: str
    reviewer_adapter: str
    issues: list[ReviewIssue] = field(default_factory=list)
    created_at: str = field(default_factory=utc_now_iso)

    @property
    def approved(self) -> bool:
        return self.decision == ReviewDecision.APPROVE

    def format_feedback(self) -> str:
        lines = [
            f"Reviewer ({self.reviewer_adapter}) decision: {self.decision.value}",
            f"Summary: {self.summary}",
        ]
        for idx, issue in enumerate(self.issues, start=1):
            loc = f"{issue.file}:{issue.line}" if issue.line else issue.file
            lines.append(
                f"{idx}. [{issue.severity}] ({issue.rule}) {loc} — {issue.message}"
            )
            if issue.fix_hint:
                lines.append(f"   Fix hint: {issue.fix_hint}")
        return "\n".join(lines)

    def to_dict(self) -> dict[str, Any]:
        return {
            "decision": self.decision.value,
            "summary": self.summary,
            "reviewer_adapter": self.reviewer_adapter,
            "issues": [i.to_dict() for i in self.issues],
            "created_at": self.created_at,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ReviewVerdict:
        return cls(
            decision=ReviewDecision(str(data["decision"])),
            summary=str(data.get("summary", "")),
            reviewer_adapter=str(data.get("reviewer_adapter", "unknown")),
            issues=[ReviewIssue.from_dict(i) for i in data.get("issues", [])],
            created_at=str(data.get("created_at", utc_now_iso())),
        )


@dataclass
class TaskExecutionState:
    task_id: str
    status: TaskStatus = TaskStatus.PENDING
    worker_adapter: str | None = None
    reviewer_adapter: str | None = None
    branch: str | None = None
    worktree_path: str | None = None
    gate_attempts: int = 0
    review_rounds: int = 0
    last_gate_passed: bool | None = None
    last_review_decision: str | None = None
    escalation_reason: str | None = None
    commit_sha: str | None = None
    updated_at: str = field(default_factory=utc_now_iso)

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["status"] = self.status.value
        return payload

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> TaskExecutionState:
        return cls(
            task_id=str(data["task_id"]),
            status=TaskStatus(str(data.get("status", TaskStatus.PENDING.value))),
            worker_adapter=data.get("worker_adapter"),
            reviewer_adapter=data.get("reviewer_adapter"),
            branch=data.get("branch"),
            worktree_path=data.get("worktree_path"),
            gate_attempts=int(data.get("gate_attempts", 0)),
            review_rounds=int(data.get("review_rounds", 0)),
            last_gate_passed=data.get("last_gate_passed"),
            last_review_decision=data.get("last_review_decision"),
            escalation_reason=data.get("escalation_reason"),
            commit_sha=data.get("commit_sha"),
            updated_at=str(data.get("updated_at", utc_now_iso())),
        )


@dataclass
class RunState:
    run_id: str
    goal: str
    repo_root: str
    base_branch: str
    mode: FactoryMode
    tasks: dict[str, TaskExecutionState] = field(default_factory=dict)
    integration_branch: str | None = None
    integrated: bool = False
    merged_to_base: bool = False
    pr_url: str | None = None
    created_at: str = field(default_factory=utc_now_iso)
    updated_at: str = field(default_factory=utc_now_iso)

    def to_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "goal": self.goal,
            "repo_root": self.repo_root,
            "base_branch": self.base_branch,
            "mode": self.mode.value,
            "tasks": {k: v.to_dict() for k, v in self.tasks.items()},
            "integration_branch": self.integration_branch,
            "integrated": self.integrated,
            "merged_to_base": self.merged_to_base,
            "pr_url": self.pr_url,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> RunState:
        return cls(
            run_id=str(data["run_id"]),
            goal=str(data["goal"]),
            repo_root=str(data["repo_root"]),
            base_branch=str(data["base_branch"]),
            mode=FactoryMode(str(data.get("mode", FactoryMode.SOFTWARE.value))),
            tasks={
                k: TaskExecutionState.from_dict(v)
                for k, v in data.get("tasks", {}).items()
            },
            integration_branch=data.get("integration_branch"),
            integrated=bool(data.get("integrated", False)),
            merged_to_base=bool(data.get("merged_to_base", False)),
            pr_url=data.get("pr_url"),
            created_at=str(data.get("created_at", utc_now_iso())),
            updated_at=str(data.get("updated_at", utc_now_iso())),
        )
