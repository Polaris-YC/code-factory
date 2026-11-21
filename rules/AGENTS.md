# Code Factory Engineering Rules (Inverse Trust Pyramid)

1. **Deterministic Constraints First**: Prefer types, linters, AST boundary checks, and executable tests over prose instructions.
2. **Evidence Before Completion**: Never claim a task is done without machine-verifiable command output (exit code `0`) from `cf gate` or the task's `done_checks`.
3. **Deletion Bias**: Favor removing or simplifying code over adding speculative abstractions or wrappers.
4. **Scope Discipline**: Only modify files within the declared `owned_paths` of the active task.
5. **Ratchet Every Mistake**: When a gate or review catches a recurring anti-pattern, record or graduate it into a permanent deterministic rule in `.code-factory/factory.toml`.
6. **Human-Readable Symbol Glossing**: In human-facing reports, reviews, and bug explanations, add a brief parenthetical role gloss (3-10 words) on first mention of any repo-internal file, class, function, or config symbol (e.g., `_remote_object_meta()` (legacy remote metadata lookup)). Do not repeat the gloss for subsequent mentions in the same reply, skip standard library/common terms, and never add these glosses as source-code comments.
