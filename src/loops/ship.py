from __future__ import annotations

import asyncio
import logging
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path

from ..budget import BudgetTracker, UsageRecord, estimate_cost, parse_token_usage
from ..config import DeployConfig, SegmentConfig, ShipLoopConfig
from ..deploy import verify_deployment
from ..git_ops import (
    commit,
    create_tag,
    get_changed_files,
    get_short_sha,
    push,
    security_scan,
    stage_files,
)
from ..learnings import LearningsEngine
from ..preflight import PreflightResult, run_preflight
from ..reporting import Reporter, SegmentReport

logger = logging.getLogger("shiploop.loop.ship")


@dataclass
class ShipResult:
    success: bool
    commit_sha: str = ""
    tag: str = ""
    deploy_url: str = ""
    report: SegmentReport | None = None


async def run_ship_loop(
    config: ShipLoopConfig,
    segment: SegmentConfig,
    segment_index: int,
    reporter: Reporter,
    budget: BudgetTracker,
    learnings: LearningsEngine,
) -> ShipResult:
    repo = Path(config.repo)
    report = SegmentReport(name=segment.name, status="running")
    loop_start = time.monotonic()

    # Phase 1: Load learnings and build augmented prompt
    relevant_learnings = learnings.search(segment.prompt)
    learnings_prefix = learnings.format_for_prompt(relevant_learnings)

    augmented_prompt = segment.prompt
    if learnings_prefix:
        augmented_prompt = learnings_prefix + "\n---\n\n" + segment.prompt
        reporter._print(f"   📚 Loaded {len(relevant_learnings)} relevant learning(s)")
    else:
        reporter._print("   📚 No prior learnings")

    # Phase 2: Run coding agent
    reporter.segment_phase(segment.name, "coding")

    if not budget.check_budget(segment.name):
        reporter.budget_halt(segment.name, budget.get_segment_cost(segment.name), config.budget.max_usd_per_segment)
        report.status = "failed"
        report.errors.append("Budget exceeded before coding")
        report.duration_seconds = time.monotonic() - loop_start
        return ShipResult(success=False, report=report)

    agent_result = await _run_agent(config.agent_command, augmented_prompt, repo)
    _record_agent_usage(budget, segment.name, "ship", agent_result)

    if not agent_result.success:
        report.status = "failed"
        report.errors.append(f"Agent failed: {agent_result.error[:200]}")
        report.duration_seconds = time.monotonic() - loop_start
        return ShipResult(success=False, report=report)

    reporter._print(f"   ✅ Agent completed in {agent_result.duration:.0f}s")

    # Phase 3: Run preflight
    reporter.segment_phase(segment.name, "preflight")
    preflight_result = await run_preflight(config.preflight, repo)

    if not preflight_result.passed:
        # Return the preflight result so the orchestrator can trigger repair
        report.duration_seconds = time.monotonic() - loop_start
        return ShipResult(success=False, report=report)

    reporter._print("   ✅ Preflight passed")

    # Phase 4: Ship (stage, scan, commit, push, tag)
    ship_result = await _ship_changes(config, segment, repo, reporter)
    if not ship_result.success:
        report.status = "failed"
        report.errors.append(f"Ship failed: {ship_result.error}")
        report.duration_seconds = time.monotonic() - loop_start
        return ShipResult(success=False, report=report)

    # Phase 5: Verify deployment
    reporter.segment_phase(segment.name, "verifying")
    verify_result = await verify_deployment(config.deploy, ship_result.commit_sha, config.site)

    if not verify_result.success:
        report.status = "failed"
        report.errors.append(f"Deploy verification failed: {verify_result.details}")
        report.duration_seconds = time.monotonic() - loop_start
        return ShipResult(
            success=False,
            commit_sha=ship_result.commit_sha,
            tag=ship_result.tag,
            report=report,
        )

    reporter._print("   ✅ Deploy verified")

    report.status = "shipped"
    report.commit = ship_result.commit_sha
    report.tag = ship_result.tag
    report.deploy_url = verify_result.deploy_url or ""
    report.cost_usd = budget.get_segment_cost(segment.name)
    report.duration_seconds = time.monotonic() - loop_start

    return ShipResult(
        success=True,
        commit_sha=ship_result.commit_sha,
        tag=ship_result.tag,
        deploy_url=verify_result.deploy_url or "",
        report=report,
    )


@dataclass
class _AgentResult:
    success: bool
    output: str = ""
    error: str = ""
    duration: float = 0.0


async def _run_agent(agent_command: str, prompt: str, cwd: Path) -> _AgentResult:
    with tempfile.NamedTemporaryFile(
        mode="w", prefix="shiploop-prompt-", suffix=".txt", delete=False,
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
                success=False,
                output=output,
                error=f"Agent exited with code {proc.returncode}",
                duration=duration,
            )
        return _AgentResult(success=True, output=output, duration=duration)
    finally:
        prompt_file.unlink(missing_ok=True)


@dataclass
class _ShipChangesResult:
    success: bool
    commit_sha: str = ""
    tag: str = ""
    error: str = ""


async def _ship_changes(
    config: ShipLoopConfig,
    segment: SegmentConfig,
    repo: Path,
    reporter: Reporter,
) -> _ShipChangesResult:
    reporter.segment_phase(segment.name, "shipping")

    changed_files = await get_changed_files(repo)
    if not changed_files:
        return _ShipChangesResult(success=False, error="No changed files to commit")

    safe_files, blocked_files = security_scan(changed_files, config.blocked_patterns)

    if blocked_files:
        blocked_list = "; ".join(blocked_files[:5])
        return _ShipChangesResult(success=False, error=f"Blocked files detected: {blocked_list}")

    if not safe_files:
        return _ShipChangesResult(success=False, error="No safe files to commit after security scan")

    await stage_files(safe_files, repo)

    commit_msg = f"feat(shiploop): {segment.name}"
    sha = await commit(commit_msg, repo)
    tag = await create_tag(segment.name, repo)

    await push(repo, include_tags=True)

    short_sha = await get_short_sha(repo)
    reporter._print(f"   📦 Committed: {short_sha}")
    reporter._print(f"   🏷  Tagged: {tag}")

    return _ShipChangesResult(success=True, commit_sha=sha, tag=tag)


def _record_agent_usage(
    budget: BudgetTracker,
    segment: str,
    loop: str,
    agent_result: _AgentResult,
) -> None:
    tokens_in, tokens_out = parse_token_usage(agent_result.output)
    cost = estimate_cost(tokens_in, tokens_out)
    budget.record_usage(UsageRecord(
        segment=segment,
        loop=loop,
        tokens_in=tokens_in,
        tokens_out=tokens_out,
        estimated_cost_usd=cost,
        duration_seconds=agent_result.duration,
    ))
