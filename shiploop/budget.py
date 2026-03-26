from __future__ import annotations

import json
import logging
import os
import re
from datetime import datetime, timezone
from pathlib import Path

from pydantic import BaseModel

from .config import BudgetConfig

logger = logging.getLogger("shiploop.budget")

MODEL_PRICING_PER_MILLION = {
    "claude-sonnet-4-6": {"input": 3.0, "output": 15.0},
    "claude-opus-4-6": {"input": 15.0, "output": 75.0},
    "claude-haiku-4-5": {"input": 0.80, "output": 4.0},
    "gpt-4o": {"input": 2.50, "output": 10.0},
    "gpt-4o-mini": {"input": 0.15, "output": 0.60},
    "default": {"input": 3.0, "output": 15.0},
}


class UsageRecord(BaseModel):
    segment: str
    loop: str
    tokens_in: int = 0
    tokens_out: int = 0
    estimated_cost_usd: float = 0.0
    timestamp: str = ""
    duration_seconds: float = 0.0


class BudgetTracker:
    def __init__(self, config: BudgetConfig, metrics_dir: Path):
        self.config = config
        self.metrics_file = metrics_dir / "metrics.json"
        self.records: list[UsageRecord] = []
        self._load()

    def _load(self) -> None:
        if self.metrics_file.exists():
            try:
                data = json.loads(self.metrics_file.read_text())
                self.records = [UsageRecord.model_validate(r) for r in data.get("records", [])]
            except (json.JSONDecodeError, Exception) as e:
                logger.warning("Failed to load metrics: %s", e)
                self.records = []

    def _save(self) -> None:
        self.metrics_file.parent.mkdir(parents=True, exist_ok=True)
        data = {"records": [r.model_dump() for r in self.records]}
        content = json.dumps(data, indent=2)
        tmp_path = self.metrics_file.with_suffix(".json.tmp")
        tmp_path.write_text(content)
        os.replace(str(tmp_path), str(self.metrics_file))

    def check_budget(self, segment: str) -> bool:
        if not self.config.halt_on_breach:
            return True
        segment_cost = self.get_segment_cost(segment)
        if segment_cost >= self.config.max_usd_per_segment:
            logger.warning(
                "Segment %s budget exceeded: $%.2f >= $%.2f",
                segment, segment_cost, self.config.max_usd_per_segment,
            )
            return False
        run_cost = self.get_run_cost()
        if run_cost >= self.config.max_usd_per_run:
            logger.warning("Run budget exceeded: $%.2f >= $%.2f", run_cost, self.config.max_usd_per_run)
            return False
        return True

    def record_usage(self, record: UsageRecord) -> None:
        if not record.timestamp:
            record.timestamp = datetime.now(timezone.utc).isoformat()
        self.records.append(record)
        self._save()

    def get_segment_cost(self, segment: str) -> float:
        return sum(r.estimated_cost_usd for r in self.records if r.segment == segment)

    def get_run_cost(self) -> float:
        return sum(r.estimated_cost_usd for r in self.records)

    def check_optimization_budget(self, segment: str) -> bool:
        optimization_cost = sum(
            r.estimated_cost_usd for r in self.records
            if r.segment == segment and r.loop.startswith("optimize-")
        )
        return optimization_cost < self.config.optimization_budget_usd

    def get_segment_tokens(self, segment: str) -> int:
        return sum(r.tokens_in + r.tokens_out for r in self.records if r.segment == segment)

    def get_summary(self) -> dict:
        by_segment: dict[str, float] = {}
        for r in self.records:
            by_segment[r.segment] = by_segment.get(r.segment, 0.0) + r.estimated_cost_usd
        return {
            "total_cost_usd": self.get_run_cost(),
            "total_records": len(self.records),
            "by_segment": by_segment,
            "budget_remaining_usd": self.config.max_usd_per_run - self.get_run_cost(),
        }


def parse_token_usage(agent_output: str) -> tuple[int, int]:
    tokens_in = 0
    tokens_out = 0
    for line in agent_output.splitlines():
        in_match = re.search(r"input[_ ]tokens?\s*[:=]\s*(\d[\d,]*)", line, re.IGNORECASE)
        if in_match:
            tokens_in = int(in_match.group(1).replace(",", ""))
        out_match = re.search(r"output[_ ]tokens?\s*[:=]\s*(\d[\d,]*)", line, re.IGNORECASE)
        if out_match:
            tokens_out = int(out_match.group(1).replace(",", ""))
    return tokens_in, tokens_out


def estimate_from_prompt(prompt_length: int, duration_seconds: float, model: str = "default") -> tuple[int, int]:
    estimated_input = max(prompt_length // 4, 100)
    estimated_output = max(int(duration_seconds * 30), 200)
    return estimated_input, estimated_output


def estimate_cost(
    tokens_in: int, tokens_out: int, model: str = "default",
    custom_pricing: dict[str, dict[str, float]] | None = None,
) -> float:
    all_pricing = {**MODEL_PRICING_PER_MILLION}
    if custom_pricing:
        all_pricing.update(custom_pricing)
    pricing = all_pricing.get(model, all_pricing["default"])
    cost = (tokens_in * pricing["input"] + tokens_out * pricing["output"]) / 1_000_000
    return round(cost, 4)
