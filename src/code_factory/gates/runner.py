"""Deterministic gate runner aggregating slop, arch, repo commands, and task done_checks."""

from __future__ import annotations

import os
import subprocess
import time
from pathlib import Path

from code_factory.config import FactoryConfig, GateCommandConfig
from code_factory.core.models import GateCheckResult, GateReport
from code_factory.gates.arch import run_arch_gate
from code_factory.gates.slop import run_slop_gate


def _tail_excerpt(stdout: str, stderr: str, max_lines: int = 40) -> str:
    combined = "\n".join(part for part in (stdout.strip(), stderr.strip()) if part)
    lines = combined.splitlines()
    if len(lines) <= max_lines:
        return combined
    return "\n".join(["... [output truncated] ...", *lines[-max_lines:]])


class GateRunner:
    def __init__(self, config: FactoryConfig) -> None:
        self.config = config

    def _run_shell_gate(
        self,
        gate: GateCommandConfig,
        cwd: Path,
        log_dir: Path | None = None,
    ) -> GateCheckResult:
        start = time.monotonic()
        env = os.environ.copy()
        env["PYTHONDONTWRITEBYTECODE"] = "1"
        try:
            proc = subprocess.run(
                gate.cmd,
                cwd=str(cwd),
                shell=True,
                capture_output=True,
                text=True,
                timeout=gate.timeout,
                env=env,
                check=False,
            )
            exit_code = proc.returncode
            stdout = proc.stdout
            stderr = proc.stderr
            timed_out = False
        except subprocess.TimeoutExpired as exc:
            exit_code = 124
            stdout = (exc.stdout or "") if isinstance(exc.stdout, str) else ""
            stderr = (exc.stderr or "") if isinstance(exc.stderr, str) else ""
            timed_out = True

        duration = round(time.monotonic() - start, 3)
        log_path: str | None = None
        if log_dir is not None:
            log_dir.mkdir(parents=True, exist_ok=True)
            safe = "".join(c if c.isalnum() or c in "-_" else "_" for c in gate.name)
            lp = log_dir / f"gate_{safe}.log"
            lp.write_text(
                f"$ {gate.cmd}\nexit={exit_code} timed_out={timed_out} duration={duration}s\n\n"
                f"--- STDOUT ---\n{stdout}\n\n--- STDERR ---\n{stderr}\n",
                encoding="utf-8",
            )
            log_path = str(lp)

        passed = exit_code == 0 and not timed_out
        summary = (
            f"Command `{gate.cmd}` passed in {duration}s."
            if passed
            else f"Command `{gate.cmd}` failed (exit={exit_code}, timed_out={timed_out})."
        )
        return GateCheckResult(
            name=gate.name,
            passed=passed,
            exit_code=exit_code,
            summary=summary,
            excerpt="" if passed else _tail_excerpt(stdout, stderr),
            duration_seconds=duration,
            log_path=log_path,
        )

    def run_all(
        self,
        *,
        cwd: Path,
        changed_files: list[str],
        diff_text: str,
        owned_paths: list[str] | None = None,
        done_checks: list[str] | None = None,
        max_diff_lines_override: int | None = None,
        log_dir: Path | None = None,
    ) -> GateReport:
        checks: list[GateCheckResult] = []

        checks.append(
            run_slop_gate(
                changed_files=changed_files,
                diff_text=diff_text,
                config=self.config.slop,
                owned_paths=owned_paths,
                max_diff_lines_override=max_diff_lines_override,
            )
        )
        checks.append(run_arch_gate(cwd, self.config.arch))

        for cmd_cfg in self.config.gate_commands:
            checks.append(self._run_shell_gate(cmd_cfg, cwd=cwd, log_dir=log_dir))

        for idx, cmd in enumerate(done_checks or [], start=1):
            checks.append(
                self._run_shell_gate(
                    GateCommandConfig(name=f"done-check-{idx}", cmd=cmd, timeout=180),
                    cwd=cwd,
                    log_dir=log_dir,
                )
            )

        return GateReport(
            passed=all(c.passed for c in checks),
            checks=checks,
        )
