from __future__ import annotations

import asyncio
import logging
import time
from pathlib import Path

from .budget import BudgetTracker
from .config import (
    SegmentConfig,
    SegmentStatus,
    ShipLoopConfig,
    load_config,
    save_config,
)
from .learnings import LearningsEngine
from .git_ops import get_diff, get_diff_stat
from .loops.meta import run_meta_loop
from .loops.optimize import run_optimization_loop
from .loops.repair import run_repair_loop
from .loops.ship import ShipResult, run_ship_loop
from .preflight import run_preflight
from .reporting import Reporter, SegmentReport

logger = logging.getLogger("shiploop.orchestrator")


class Orchestrator:
    def __init__(self, config_path: Path):
        self.config_path = config_path
        self.config = load_config(config_path)
        self.repo = Path(self.config.repo)

        metrics_dir = self.repo / ".shiploop"
        self.budget = BudgetTracker(self.config.budget, metrics_dir)

        learnings_path = self.repo / "learnings.yml"
        self.learnings = LearningsEngine(learnings_path)

        self.reporter = Reporter(self.config)
        self._optimization_tasks: list[asyncio.Task] = []

    def _checkpoint(self) -> None:
        save_config(self.config, self.config_path)

    def _set_segment_status(self, segment: SegmentConfig, status: SegmentStatus) -> None:
        segment.status = status
        self._checkpoint()

    def _find_eligible_segments(self) -> list[int]:
        shipped = {s.name for s in self.config.segments if s.status == SegmentStatus.SHIPPED}
        eligible: list[int] = []

        for i, seg in enumerate(self.config.segments):
            if seg.status != SegmentStatus.PENDING:
                continue
            deps_met = all(dep in shipped for dep in seg.depends_on)
            if deps_met:
                eligible.append(i)

        return eligible

    def _recover_crashed_segments(self) -> list[str]:
        recovered: list[str] = []
        for seg in self.config.segments:
            if seg.status in SegmentStatus.active_states():
                logger.warning("Crash recovery: segment '%s' was in state '%s', marking failed", seg.name, seg.status.value)
                self._set_segment_status(seg, SegmentStatus.FAILED)
                recovered.append(seg.name)
        return recovered

    async def run(self) -> bool:
        self.reporter.pipeline_start()

        recovered = self._recover_crashed_segments()
        if recovered:
            for name in recovered:
                self.reporter._print(f"⚠️  Crash recovery: '{name}' was in-progress, marked failed")

        all_success = True

        while True:
            eligible = self._find_eligible_segments()
            if not eligible:
                break

            for seg_index in eligible:
                segment = self.config.segments[seg_index]
                success = await self._run_segment(seg_index, segment)
                if not success:
                    all_success = False

        if self._optimization_tasks:
            await asyncio.gather(*self._optimization_tasks, return_exceptions=True)

        self.reporter.pipeline_complete()
        return all_success

    async def _run_segment(self, index: int, segment: SegmentConfig) -> bool:
        self.reporter.segment_start(index, segment)

        # Phase 1: Ship loop (code → preflight → ship → verify)
        self._set_segment_status(segment, SegmentStatus.CODING)

        ship_result = await run_ship_loop(
            self.config, segment, index,
            self.reporter, self.budget, self.learnings,
        )

        if ship_result.success:
            return self._mark_shipped(index, segment, ship_result)

        # If agent failed (not preflight), bail
        report = ship_result.report
        if report and report.errors and "Agent failed" in report.errors[0]:
            return self._mark_failed(index, segment, ship_result)

        # Phase 2: Repair loop (preflight failed)
        self._set_segment_status(segment, SegmentStatus.REPAIRING)
        self.reporter._print("   ❌ Preflight FAILED — entering repair loop")

        preflight_result = await run_preflight(self.config.preflight, Path(self.config.repo))
        repair_result = await run_repair_loop(
            self.config, segment.name, preflight_result,
            self.reporter, self.budget, self.learnings,
        )

        if repair_result.success:
            repair_diff = await get_diff(Path(self.config.repo))
            repair_diff_stat = await get_diff_stat(Path(self.config.repo))
            repair_diff_lines = len(repair_diff_stat.strip().splitlines()) if repair_diff_stat.strip() else 0

            self._set_segment_status(segment, SegmentStatus.SHIPPING)
            ship_result = await self._ship_and_verify(segment)
            if ship_result.success:
                result = self._mark_shipped(index, segment, ship_result)

                task = asyncio.create_task(self._run_optimization(
                    segment, preflight_result.combined_output,
                    repair_diff, repair_result.attempts_used, repair_diff_lines,
                ))
                self._optimization_tasks.append(task)

                return result
            return self._mark_failed(index, segment, ship_result)

        # Phase 3: Meta loop (repair exhausted)
        self._set_segment_status(segment, SegmentStatus.EXPERIMENTING)
        all_errors = [preflight_result.combined_output]

        meta_result = await run_meta_loop(
            self.config, segment.name, segment.prompt,
            all_errors, self.reporter, self.budget, self.learnings,
        )

        if meta_result.success:
            # Meta loop merged a winner — ship the changes
            self._set_segment_status(segment, SegmentStatus.SHIPPING)
            ship_result = await self._ship_and_verify(segment)
            if ship_result.success:
                return self._mark_shipped(index, segment, ship_result)
            return self._mark_failed(index, segment, ship_result)

        return self._mark_failed(index, segment, ShipResult(success=False, report=SegmentReport(
            name=segment.name, status="failed",
            repair_attempts=repair_result.attempts_used,
            meta_experiments=meta_result.experiments_tried,
            errors=["All loops exhausted"],
        )))

    async def _ship_and_verify(self, segment: SegmentConfig) -> ShipResult:
        from .deploy import verify_deployment
        from .git_ops import (
            commit,
            create_tag,
            get_changed_files,
            get_short_sha,
            push,
            security_scan,
            stage_files,
        )

        repo = Path(self.config.repo)

        changed_files = await get_changed_files(repo)
        if not changed_files:
            return ShipResult(success=False, report=SegmentReport(
                name=segment.name, status="failed", errors=["No changed files"],
            ))

        safe_files, blocked_files = security_scan(changed_files, self.config.blocked_patterns)
        if blocked_files:
            return ShipResult(success=False, report=SegmentReport(
                name=segment.name, status="failed",
                errors=[f"Blocked: {', '.join(blocked_files[:3])}"],
            ))

        if not safe_files:
            return ShipResult(success=False, report=SegmentReport(
                name=segment.name, status="failed", errors=["No safe files after scan"],
            ))

        await stage_files(safe_files, repo)
        sha = await commit(f"feat(shiploop): {segment.name}", repo)
        tag = await create_tag(segment.name, repo)
        await push(repo, include_tags=True)

        short_sha = await get_short_sha(repo)
        self.reporter._print(f"   📦 Committed: {short_sha}")
        self.reporter._print(f"   🏷  Tagged: {tag}")

        self._set_segment_status(segment, SegmentStatus.VERIFYING)
        verify_result = await verify_deployment(self.config.deploy, sha, self.config.site)

        if verify_result.success:
            self.reporter._print("   ✅ Deploy verified")
            return ShipResult(
                success=True, commit_sha=sha, tag=tag,
                deploy_url=verify_result.deploy_url or "",
                report=SegmentReport(
                    name=segment.name, status="shipped",
                    commit=sha, tag=tag, deploy_url=verify_result.deploy_url,
                ),
            )

        return ShipResult(success=False, commit_sha=sha, tag=tag, report=SegmentReport(
            name=segment.name, status="failed",
            errors=[f"Deploy verification failed: {verify_result.details}"],
        ))

    def _mark_shipped(self, index: int, segment: SegmentConfig, result: ShipResult) -> bool:
        segment.commit = result.commit_sha
        segment.tag = result.tag
        segment.deploy_url = result.deploy_url
        self._set_segment_status(segment, SegmentStatus.SHIPPED)

        report = result.report or SegmentReport(
            name=segment.name, status="shipped",
            commit=result.commit_sha, tag=result.tag,
            cost_usd=self.budget.get_segment_cost(segment.name),
        )
        report.cost_usd = self.budget.get_segment_cost(segment.name)
        self.reporter.segment_shipped(index, report)
        return True

    def _mark_failed(self, index: int, segment: SegmentConfig, result: ShipResult) -> bool:
        self._set_segment_status(segment, SegmentStatus.FAILED)

        report = result.report or SegmentReport(name=segment.name, status="failed")
        self.reporter.segment_failed(index, report)
        return False

    async def _run_optimization(
        self,
        segment: SegmentConfig,
        preflight_error: str,
        repair_diff: str,
        repair_attempts: int,
        repair_diff_lines: int,
    ) -> None:
        try:
            await run_optimization_loop(
                self.config,
                segment.name,
                segment.prompt,
                preflight_error,
                repair_diff,
                repair_attempts,
                repair_diff_lines,
                self.reporter,
                self.budget,
                self.learnings,
            )
        except Exception as e:
            logger.error("Optimization for '%s' failed: %s", segment.name, e)

    def get_status(self) -> list[dict]:
        return [
            {
                "name": seg.name,
                "status": seg.status.value,
                "commit": seg.commit,
                "tag": seg.tag,
                "deploy_url": seg.deploy_url,
                "depends_on": seg.depends_on,
            }
            for seg in self.config.segments
        ]

    def reset_segment(self, segment_name: str) -> bool:
        for seg in self.config.segments:
            if seg.name == segment_name:
                seg.status = SegmentStatus.PENDING
                seg.commit = None
                seg.tag = None
                seg.deploy_url = None
                self._checkpoint()
                return True
        return False
