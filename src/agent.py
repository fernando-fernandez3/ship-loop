from __future__ import annotations

import asyncio
import logging
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path

from .budget import BudgetTracker, UsageRecord, estimate_cost, estimate_from_prompt, parse_token_usage

logger = logging.getLogger("shiploop.agent")


@dataclass
class AgentResult:
    success: bool
    output: str = ""
    error: str = ""
    duration: float = 0.0


async def run_agent(
    agent_command: str, prompt: str, cwd: Path, timeout: int = 900,
) -> AgentResult:
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
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        duration = time.monotonic() - start
        output = stdout.decode(errors="replace")

        if proc.returncode != 0:
            return AgentResult(
                success=False,
                output=output,
                error=f"Agent exited with code {proc.returncode}",
                duration=duration,
            )
        return AgentResult(success=True, output=output, duration=duration)
    except asyncio.TimeoutError:
        duration = time.monotonic() - start
        try:
            proc.kill()
        except ProcessLookupError:
            pass
        return AgentResult(
            success=False,
            output="",
            error=f"Agent timed out after {timeout}s",
            duration=duration,
        )
    finally:
        prompt_file.unlink(missing_ok=True)


def record_agent_usage(
    budget: BudgetTracker,
    segment: str,
    loop: str,
    agent_result: AgentResult,
) -> None:
    tokens_in, tokens_out = parse_token_usage(agent_result.output)
    if tokens_in == 0 and tokens_out == 0:
        tokens_in, tokens_out = estimate_from_prompt(
            len(agent_result.output), agent_result.duration,
        )
    cost = estimate_cost(tokens_in, tokens_out)
    budget.record_usage(UsageRecord(
        segment=segment,
        loop=loop,
        tokens_in=tokens_in,
        tokens_out=tokens_out,
        estimated_cost_usd=cost,
        duration_seconds=agent_result.duration,
    ))
