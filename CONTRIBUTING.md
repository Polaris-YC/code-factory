# Contributing to Code Factory

Code Factory targets Python 3.12+ and uses only the standard library at runtime
and in its test suite. Git is required for worktree operations. Keep checks
deterministic and runnable without API keys.

## Run the gates locally

From the repository root, run `cf gate --repo .` with the installed CLI, or use
the equivalent command directly from the checkout.

```sh
PYTHONPATH=src python3 -m code_factory gate --repo .
```

## Import boundaries

`arch-gate` enforces four rules in [`.code-factory/factory.toml`](.code-factory/factory.toml).
Each name in the table is relative to the `code_factory` package.

| Module | Forbidden imports |
| --- | --- |
| `core.models` | `adapters`, `gates`, `core.orchestrator`, `core.worktree`, `core.state`, `config`, `cli` |
| `adapters` | `gates`, `core.orchestrator`, `core.worktree`, `cli` |
| `gates` | `adapters`, `core.orchestrator`, `cli` |
| `core.worktree` | `adapters`, `gates`, `core.orchestrator`, `cli` |

## Add an adapter or gate

- For a non-interactive CLI, define `[adapters.<name>]` with `type = "generic"`,
  `command`, and `prompt_mode` in `.code-factory/factory.toml`. Add its name to
  the relevant `[routing]` lists. For CLI-specific behavior, subclass
  `BaseAdapter` in `src/code_factory/adapters/` and implement `build_command`.
  Override `parse_final_message` if needed and register the type in
  `_ADAPTER_TYPES` in `src/code_factory/adapters/registry.py`.
- For a repository-wide command gate, add a `name`, `cmd`, and `timeout` entry
  to `[gates].commands`. Use `done_checks` for task-specific commands. For a new
  built-in gate, return a `GateCheckResult` and register the check in
  `GateRunner.run_all` in `src/code_factory/gates/runner.py`.

## Pull request checklist

- [ ] Keep changes within the declared scope and preserve the import boundaries.
- [ ] Cover behavior changes with `unittest` and run `cf gate --repo .`.
- [ ] Run the offline quickstart from the repository root.

  ```sh
  PYTHONPATH=src python3 -W error::ResourceWarning examples/quickstart/run_mock_demo.py
  ```

- [ ] Describe the change and include the verification results in the PR.
