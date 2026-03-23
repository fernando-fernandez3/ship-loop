from __future__ import annotations

import asyncio
import hashlib
import logging
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path

from ..budget import BudgetTracker, UsageRecord, estimate_cost, parse_token_usage
from ..config import RepairConfig, ShipLoopConfig
from ..git_ops import get_diff, get_changed_files
from ..learnings import LearningsEngine
from ..preflight import PreflightResult, run_preflight
from ..reporting import Reporter

logger = logging.getLogger("shiploop.loop.repair")


@dataclass
class RepairResult:
    success: bool
    attempts_used: int = 0
    converged: bool = False


async def run_repair_loop(
    config: ShipLoopConfig,
    segment_name: str,
    initial_preflight: PreflightResult,
    reporter: Reporter,
    budget: BudgetTracker,
    learnings: LearningsEngine,
) -> RepairResult:
    repo = Path(config.repo)
    max_attempts = config.repair.max_attempts
    error_signatures: list[str] = []
    last_preflight = initial_preflight

    for attempt in range(1, max_attempts + 1):
        reporter.repair_attempt(segment_name, attempt, max_attempts)

        error_sig = _compute_error_signature(last_preflight.combined_output)
        error_signatures.append(error_sig)

        if len(error_signatures) >= 2 and error_signatures[-1] == error_signatures[-2]:
            reporter._print("   ⚠️  Convergence detected: same error twice in a row")
            return RepairResult(success=False, attempts_used=attempt, converged=True)

        if not budget.check_budget(segment_name):
            reporter.budget_halt(
                segment_name,
                budget.get_segment_cost(segment_name),
                config.budget.max_usd_per_segment,
            )
            return RepairResult(success=False, attempts_used=attempt)

        repair_prompt = _build_repair_prompt(
            segment_name, attempt, last_preflight, repo,
        )

        agent_result = await _run_repair_agent(config.agent_command, repair_prompt, repo)
        _record_repair_usage(budget, segment_name, attempt, agent_result)

        if not agent_result.success:
            reporter.repair_failure(segment_name, attempt, f"Agent failed: {agent_result.error[:100]}")
            continue

        preflight_result = await run_preflight(config.preflight, repo)

        if preflight_result.passed:
            reporter.repair_success(segment_name, attempt)

            _record_repair_learning(
                learnings, segment_name, last_preflight, attempt,
            )
            return RepairResult(success=True, attempts_used=attempt)

        reporter.repair_failure(segment_name, attempt, preflight_result.errors[0] if preflight_result.errors else "unknown")
        last_preflight = preflight_result

    return RepairResult(success=False, attempts_used=max_attempts)


def _compute_error_signature(error_text: str) -> str:
    lines = error_text.strip().splitlines()[:5]
    content = "\n".join(lines)
    return hashlib.sha256(content.encode()).hexdigest()[:16]


def _build_repair_prompt(
    segment_name: str,
    attempt: int,
    preflight: PreflightResult,
    repo: Path,
) -> str:
    error_output = preflight.combined_output
    last_lines = "\n".join(error_output.splitlines()[-100:]) if error_output else "(no output)"

    return f"""## REPAIR ATTEMPT {attempt} for segment: {segment_name}

The previous attempt to implement this segment failed. Your job is to FIX the issues
so the code builds, lints, and tests cleanly.

### Failed Step
{preflight.failed_step}

### Error Output
```
{last_lines}
```

### Instructions
1. Read the error output carefully
2. Identify the root cause
3. Fix the code — do NOT start over from scratch unless the approach is fundamentally broken
4. Ensure the project builds, lints, and tests pass
5. Do NOT remove or disable tests to make them pass
"""


@dataclass
class _AgentResult:
    success: bool
    output: str = ""
    error: str = ""
    duration: float = 0.0


async def _run_repair_agent(agent_command: str, prompt: str, cwd: Path) -> _AgentResult:
    with tempfile.NamedTemporaryFile(
        mode="w", prefix="shiploop-repair-", suffix=".txt", delete=False,
    ) as f:
        f.write(prompt)
        prompt_file = Path(f.name)

    try:
        start = time.monotonic()
        proc = await asyncio.create_subprocess_shell(
            f"cat {prompt_file} | {agent_command}",
            cwd=cwd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
        stdout, _ = await proc.communicate()
        duration = time.monotonic() - start
        output = stdout.decode(errors="replace")

        if proc.returncode != 0:
            return _AgentResult(
                success=False, output=output,
                error=f"Exit code {proc.returncode}", duration=duration,
            )
        return _AgentResult(success=True, output=output, duration=duration)
    finally:
        prompt_file.unlink(missing_ok=True)


def _record_repair_usage(
    budget: BudgetTracker, segment: str, attempt: int, result: _AgentResult,
) -> None:
    tokens_in, tokens_out = parse_token_usage(result.output)
    cost = estimate_cost(tokens_in, tokens_out)
    budget.record_usage(UsageRecord(
        segment=segment,
        loop=f"repair-{attempt}",
        tokens_in=tokens_in,
        tokens_out=tokens_out,
        estimated_cost_usd=cost,
        duration_seconds=result.duration,
    ))


def _record_repair_learning(
    learnings: LearningsEngine,
    segment_name: str,
    failed_preflight: PreflightResult,
    attempt: int,
) -> None:
    error_summary = failed_preflight.errors[0] if failed_preflight.errors else "unknown error"
    learnings.record(
        segment=segment_name,
        failure=error_summary,
        root_cause=f"Fixed by repair loop attempt {attempt}",
        fix=f"Repair agent auto-fixed on attempt {attempt}",
        tags=["repair", f"attempt-{attempt}"],
    )
