# Task DAG Schema (`tasks.json`)

Every Code Factory run consumes a validated JSON Task DAG:

```json
{
  "run_id": "run-20261120-auth",
  "goal": "Add rate limiting middleware and unit tests",
  "base_branch": "main",
  "mode": "software",
  "tasks": [
    {
      "id": "rate-limiter-core",
      "title": "Implement token-bucket rate limiter",
      "description": "Create src/middleware/rate_limit.py with deterministic clock injection and unit tests.",
      "depends_on": [],
      "owned_paths": ["src/middleware/rate_limit.py", "tests/test_rate_limit.py"],
      "done_checks": ["python3 -m unittest tests/test_rate_limit.py -v"],
      "preferred_adapter": "codex",
      "reviewer_adapter": "claude",
      "max_diff_lines": 300
    },
    {
      "id": "wire-middleware",
      "title": "Register rate limiter in app router",
      "description": "Wire rate_limit middleware into src/app.py and add integration test.",
      "depends_on": ["rate-limiter-core"],
      "owned_paths": ["src/app.py", "tests/test_app_integration.py"],
      "done_checks": ["python3 -m unittest tests/test_app_integration.py -v"]
    }
  ]
}
```

## Validation Invariants (Enforced by `planner.py`)
1. `tasks` must be non-empty with unique, non-empty `id` values.
2. `depends_on` must reference valid task IDs with zero cycles.
3. Tasks in the same topological wave (able to run in parallel) **must have disjoint `owned_paths`**. If two tasks need the same directory or file, connect them with `depends_on`.
4. `mode` is `"software"` (default) or `"dark"` (opt-in auto-merge after all gates + cross-agent review pass).
