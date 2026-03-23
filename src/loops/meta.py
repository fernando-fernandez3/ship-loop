from __future__ import annotations

import asyncio
import logging
import re
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path

from ..budget import BudgetTracker, UsageRecord, estimate_cost, parse_token_usage
from ..config import MetaConfig, ShipLoopConfig
from ..git_ops import (
    checkout,
    create_worktree,
    delete_branch,
    discard_changes,
    get_current_branch,
    merge_branch,
    remove_worktree,
)
from ..learnings import LearningsEngine
from ..preflight import PreflightResult, run_preflight
from ..reporting import Reporter

logger = logging.getLogger("shiploop.loop.meta")


@dataclass
class MetaResult:
    success: bool
    winner_experiment: int | None = None
    winner_branch: str = ""
    experiments_tried: int = 0


async def run_meta_loop(
    config: ShipLoopConfig,
    segment_name: str,
    segment_prompt: str,
    all_errors: list[str],
    reporter: Reporter,
    budget: BudgetTracker,
    learnings: LearningsEngine,
) -> MetaResult:
    repo = Path(config.repo)
    meta_config = config.meta
    num_experiments = meta_config.experiments

    if not meta_config.enabled:
        reporter._print("   Meta loop disabled — segment failed")
        return MetaResult(success=False)

    reporter.meta_start()

    # Discard uncommitted changes from failed repair attempts
    await discard_changes(repo)

    original_branch = await get_current_branch(repo)

    # Step 1: Meta-analysis — ask agent WHY everything fails
    reporter.meta_analysis()
    failure_context = _build_failure_context(segment_name, segment_prompt, all_errors)
    meta_prompt = _build_meta_prompt(segment_name, num_experiments, failure_context)

    meta_agent = await _run_agent(config.agent_command, meta_prompt, repo)
    _record_usage(budget, segment_name, "meta-analysis", meta_agent)

    if not meta_agent.success:
        reporter._print("   ❌ Meta-analysis agent failed")
        return MetaResult(success=False)

    reporter._print("   ✅ Meta-analysis complete")

    # Step 2: Parse experiment prompts
    experiment_prompts = _parse_experiments(meta_agent.output, num_experiments, segment_prompt, failure_context)

    # Step 3: Run experiments
    candidates: list[tuple[int, int]] = []  # (experiment_num, diff_lines)

    for exp_num in range(1, num_experiments + 1):
        reporter.experiment_start(exp_num, num_experiments)

        if not budget.check_budget(segment_name):
            reporter.budget_halt(
                segment_name,
                budget.get_segment_cost(segment_name),
                config.budget.max_usd_per_segment,
            )
            break

        exp_prompt = experiment_prompts.get(exp_num, "")
        if not exp_prompt:
            reporter._print(f"   ⚠️  No prompt for experiment {exp_num}, skipping")
            continue

        branch_name = f"experiment/{segment_name}-{exp_num}"
        worktree_path = repo / f".shiploop-exp-{segment_name}-{exp_num}"

        try:
            await create_worktree(repo, branch_name, worktree_path)

            agent_result = await _run_agent(config.agent_command, exp_prompt, worktree_path)
            _record_usage(budget, segment_name, f"experiment-{exp_num}", agent_result)

            if not agent_result.success:
                reporter.experiment_result(exp_num, False)
                continue

            preflight_result = await run_preflight(config.preflight, worktree_path)

            if preflight_result.passed:
                diff_lines = await _count_diff_lines(worktree_path)
                candidates.append((exp_num, diff_lines))
                reporter.experiment_result(exp_num, True, diff_lines)
            else:
                reporter.experiment_result(exp_num, False)

        except Exception as e:
            logger.error("Experiment %d error: %s", exp_num, e)
            reporter.experiment_result(exp_num, False)
        finally:
            await remove_worktree(repo, worktree_path)

    # Step 4: Pick winner
    await checkout(repo, original_branch)

    if not candidates:
        reporter._print(f"\n   ❌ ALL {num_experiments} experiments failed — segment failed")

        learnings.record(
            segment=segment_name,
            failure="All repair and meta-experiment attempts failed",
            root_cause="Task may need decomposition or human intervention",
            fix="No automated fix found",
            tags=["meta-failure", "needs-human"],
        )

        # Cleanup experiment branches
        for exp_num in range(1, num_experiments + 1):
            await delete_branch(repo, f"experiment/{segment_name}-{exp_num}")

        return MetaResult(success=False, experiments_tried=num_experiments)

    # Pick simplest diff as tiebreaker
    candidates.sort(key=lambda x: x[1])
    winner_num, winner_diff = candidates[0]
    winner_branch = f"experiment/{segment_name}-{winner_num}"

    reporter.experiment_winner(winner_num, winner_branch)

    merged = await merge_branch(
        repo, winner_branch,
        f"feat(shiploop): {segment_name} via meta-experiment {winner_num}",
    )

    if not merged:
        reporter._print(f"   ❌ Merge conflict — winner branch preserved: {winner_branch}")
        return MetaResult(success=False, winner_experiment=winner_num, experiments_tried=num_experiments)

    learnings.record(
        segment=segment_name,
        failure=f"Repair loop exhausted",
        root_cause="Required meta-analysis and experiment branching",
        fix=f"Experiment {winner_num} succeeded with alternative approach",
        tags=["meta-success", f"experiment-{winner_num}"],
    )

    # Cleanup experiment branches
    for exp_num in range(1, num_experiments + 1):
        await delete_branch(repo, f"experiment/{segment_name}-{exp_num}")

    reporter._print("   ✅ Winner merged, experiment branches cleaned")

    return MetaResult(
        success=True,
        winner_experiment=winner_num,
        winner_branch=winner_branch,
        experiments_tried=num_experiments,
    )


def _build_failure_context(segment_name: str, prompt: str, all_errors: list[str]) -> str:
    lines = [
        f"# Failure History for: {segment_name}",
        "",
        "## Original Prompt",
        prompt,
        "",
        "## Error History from All Attempts",
    ]
    for i, error in enumerate(all_errors, 1):
        truncated = error[:500] if len(error) > 500 else error
        lines.append(f"\n### Attempt {i}")
        lines.append(f"```\n{truncated}\n```")
    return "\n".join(lines)


def _build_meta_prompt(segment_name: str, num_experiments: int, failure_context: str) -> str:
    return f"""## META-ANALYSIS: Why does segment "{segment_name}" keep failing?

All repair attempts failed. Analyze the failure history below and:

1. Identify the ROOT CAUSE — not the symptom, the underlying issue
2. Propose {num_experiments} different approaches to solve this, each fundamentally different
3. For each approach, write a COMPLETE implementation prompt

Output format (exactly):
---APPROACH 1---
<complete prompt for approach 1>
---APPROACH 2---
<complete prompt for approach 2>
---APPROACH {num_experiments}---
<complete prompt for approach {num_experiments}>

{failure_context}
"""


def _parse_experiments(
    meta_output: str,
    num_experiments: int,
    original_prompt: str,
    failure_context: str,
) -> dict[int, str]:
    experiments: dict[int, str] = {}

    for exp_num in range(1, num_experiments + 1):
        pattern = rf"---\s*APPROACH\s+{exp_num}\s*---\s*\n(.*?)(?=---\s*APPROACH\s+\d+\s*---|$)"
        match = re.search(pattern, meta_output, re.DOTALL | re.IGNORECASE)

        if match:
            prompt_text = match.group(1).strip()
            prompt_text = re.sub(r"^```\s*\n?|```\s*$", "", prompt_text).strip()
            if prompt_text:
                experiments[exp_num] = prompt_text
                continue

        experiments[exp_num] = f"""## Alternative approach {exp_num}

The standard approach failed multiple times. Try a fundamentally different strategy.

Original task:
{original_prompt}

Previous failures (summary):
{failure_context[-500:]}

Use approach {exp_num}: try a fundamentally different implementation strategy.
"""

    return experiments


async def _count_diff_lines(cwd: Path) -> int:
    proc = await asyncio.create_subprocess_exec(
        "git", "diff", "--stat", "HEAD~1",
        cwd=cwd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, _ = await proc.communicate()
    lines = stdout.decode().strip().splitlines()
    return len(lines) if lines else 999


@dataclass
class _AgentResult:
    success: bool
    output: str = ""
    error: str = ""
    duration: float = 0.0


async def _run_agent(agent_command: str, prompt: str, cwd: Path) -> _AgentResult:
    with tempfile.NamedTemporaryFile(
        mode="w", prefix="shiploop-meta-", suffix=".txt", delete=False,
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


def _record_usage(
    budget: BudgetTracker, segment: str, loop: str, result: _AgentResult,
) -> None:
    tokens_in, tokens_out = parse_token_usage(result.output)
    cost = estimate_cost(tokens_in, tokens_out)
    budget.record_usage(UsageRecord(
        segment=segment,
        loop=loop,
        tokens_in=tokens_in,
        tokens_out=tokens_out,
        estimated_cost_usd=cost,
        duration_seconds=result.duration,
    ))
