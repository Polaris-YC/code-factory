"""Run the offline quickstart with PYTHONPATH=src python3 examples/quickstart/run_mock_demo.py."""

import shlex
import subprocess
import sys
from pathlib import Path
from tempfile import TemporaryDirectory

from code_factory.adapters.mock import MockAdapter
from code_factory.adapters.registry import AdapterRegistry
from code_factory.config import FactoryConfig, GateCommandConfig
from code_factory.core.models import AgentInvocation, FactoryMode, TaskDAG, TaskSpec
from code_factory.core.orchestrator import FactoryOrchestrator


def init_repo(repo: Path) -> None:
    commands = (
        ["init", "--quiet", "--initial-branch=main", "--template="],
        ["config", "core.hooksPath", str(repo / ".git" / "hooks")],
        ["config", "commit.gpgSign", "false"],
        [
            "-c", "user.name=Code Factory Demo",
            "-c", "user.email=demo@example.com",
            "commit", "--quiet", "--allow-empty", "-m", "Start demo",
        ],
    )
    for args in commands:
        subprocess.run(["git", *args], cwd=repo, check=True)


def run_demo(repo: Path) -> int:
    init_repo(repo)
    print("1. Created a temporary Git repository on main. Both agents are offline mocks.")

    def write_greeting(invocation: AgentInvocation, attempt: int) -> tuple[int, str, None]:
        worktree = Path(invocation.cwd)
        if worktree == repo or not (worktree / ".git").is_file():
            raise RuntimeError("Expected the worker to run in a linked Git worktree.")
        if attempt == 1:
            print(f"2. {worker.name} is editing in the isolated {worktree.name} worktree.")
            greeting = "Goodbye, Code Factory!"
        elif "Gate FAILED: greeting-test" in invocation.prompt:
            print("3. The greeting-test gate rejected the first draft.")
            print("4. The worker received the gate failure and is retrying with the fix.")
            greeting = "Hello, Code Factory!"
        else:
            return 1, "Expected greeting-test diagnostics before retrying.", None
        (worktree / "greeting.py").write_text(
            f"def greet():\n    return {greeting!r}\n", encoding="utf-8"
        )
        return 0, "Wrote greeting.py", None

    worker = MockAdapter("mock-worker", handler=write_greeting)
    reviewer = MockAdapter("mock-reviewer")
    registry = AdapterRegistry(
        adapters={worker.name: worker, reviewer.name: reviewer},
        routing={"worker": [worker.name], "reviewer": [worker.name, reviewer.name]},
    )
    greeting_test = shlex.join([
        sys.executable, "-E", "-B", "-c",
        "from greeting import greet; assert greet() == 'Hello, Code Factory!'",
    ])
    orchestrator = FactoryOrchestrator(
        repo_root=repo,
        config=FactoryConfig(
            max_gate_retries=1,
            max_review_rounds=1,
            gate_commands=[GateCommandConfig(name="greeting-test", cmd=greeting_test)],
        ),
        registry=registry,
    )
    task = TaskSpec(
        id="greeting",
        title="Add a greeting",
        description="Implement greet() in greeting.py to return 'Hello, Code Factory!'.",
        owned_paths=["greeting.py"],
    )
    dag = TaskDAG(
        run_id="mock-demo",
        goal=task.title,
        base_branch="main",
        tasks=[task],
        mode=FactoryMode.DARK,
    )
    state = orchestrator.run_dag(dag)
    result = state.tasks[task.id]
    if not state.integrated or not state.merged_to_base:
        print(result.escalation_reason or "The demo did not integrate into main.", file=sys.stderr)
        return 1
    if result.gate_attempts != 2 or result.reviewer_adapter != reviewer.name:
        print("Expected one gate retry and approval from the separate mock reviewer.", file=sys.stderr)
        return 1

    print(f"5. Gates passed on attempt {result.gate_attempts}. {reviewer.name} approved the change.")
    print(f"6. Integration gates passed on {state.integration_branch} and Git merged it into main.")
    return 0


def main() -> int:
    with TemporaryDirectory(prefix="code-factory-demo-") as directory:
        return run_demo(Path(directory).resolve())


if __name__ == "__main__":
    sys.exit(main())
