# Code Factory Playbooks

Use these lean templates when decomposing common engineering requests into a `TaskDAG`:

## 1. Feature Playbook
1. **Wave 1 (Parallel)**: Core domain/data layer (`owned_paths: ["src/domain/..."]`) + independent UI/CLI/adapter modules (`owned_paths: ["src/adapters/..."]`).
2. **Wave 2 (Integration)**: Wire modules together + end-to-end tests (`depends_on: [...]`).
3. **Done Checks**: Targeted unit test command per task + full suite on integration.

## 2. Bugfix Playbook
1. **Wave 1**: Reproduction test + minimal root-cause fix (`owned_paths` strictly scoped to offending module + its test file).
2. **Done Checks**: Reproduction test fails before fix (verified in worker reasoning) and passes after fix; zero unrelated edits.
3. **Ratchet**: After fix lands, run `factory-ratchet` to check if a lint/AST/slop rule can prevent the bug class permanently.

## 3. Refactor Playbook
1. **Wave 1**: Characterization / contract tests locking current behavior (if coverage is weak).
2. **Wave 2**: Structural refactor with strict deletion bias (`max_diff_lines` budgeted).
3. **Done Checks**: All existing + new contract tests pass; `arch-gate` enforces the new boundary.
