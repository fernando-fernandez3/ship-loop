from __future__ import annotations

import hashlib
import json
import logging
import re
from datetime import datetime, timezone
from pathlib import Path

import yaml
from pydantic import BaseModel

logger = logging.getLogger("shiploop.learnings")

STOP_WORDS = frozenset(
    "a an the is are was were to from in on of for with by at and or but not "
    "this that it be have has had do does did will would should could may can".split()
)


class Learning(BaseModel):
    id: str
    date: str
    segment: str
    error_signature: str = ""
    failure: str
    root_cause: str
    fix: str
    tags: list[str] = []
    learning_type: str = "failure"
    improvement_type: str = ""
    prompt_delta: str = ""


class LearningsEngine:
    def __init__(self, learnings_path: Path):
        self.path = learnings_path
        self.learnings: list[Learning] = []
        self._load()

    def _load(self) -> None:
        if not self.path.exists():
            self.learnings = []
            return
        try:
            raw = yaml.safe_load(self.path.read_text())
            if isinstance(raw, list):
                self.learnings = [Learning.model_validate(item) for item in raw]
            else:
                self.learnings = []
        except Exception as e:
            logger.warning("Failed to load learnings: %s", e)
            self.learnings = []

    def _save(self) -> None:
        import os
        data = [learning.model_dump() for learning in self.learnings]
        content = yaml.dump(data, default_flow_style=False, sort_keys=False, allow_unicode=True)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = self.path.with_suffix(".yml.tmp")
        tmp_path.write_text(content)
        os.replace(str(tmp_path), str(self.path))

    def record(
        self,
        segment: str,
        failure: str,
        root_cause: str,
        fix: str,
        tags: list[str] | None = None,
    ) -> Learning:
        next_id = f"L{len(self.learnings) + 1:03d}"
        error_sig = _compute_error_signature(failure)
        auto_tags = _extract_tags(failure + " " + root_cause + " " + fix)
        all_tags = sorted(set((tags or []) + auto_tags))

        learning = Learning(
            id=next_id,
            date=datetime.now(timezone.utc).strftime("%Y-%m-%d"),
            segment=segment,
            error_signature=error_sig,
            failure=failure[:500],
            root_cause=root_cause[:500],
            fix=fix[:500],
            tags=all_tags,
        )
        self.learnings.append(learning)
        self._save()
        logger.info("Recorded learning %s for segment %s", next_id, segment)
        return learning

    def search(self, query: str, max_results: int = 3) -> list[Learning]:
        if not self.learnings:
            return []

        query_keywords = _extract_keywords(query)
        if not query_keywords:
            return self.learnings[:max_results]

        scored: list[tuple[float, Learning]] = []
        for learning in self.learnings:
            score = _keyword_score(query_keywords, learning)
            if score > 0:
                scored.append((score, learning))

        scored.sort(key=lambda x: x[0], reverse=True)
        return [learning for _, learning in scored[:max_results]]

    def format_for_prompt(self, learnings: list[Learning]) -> str:
        if not learnings:
            return ""

        failure_learnings = [l for l in learnings if l.learning_type != "optimization"]
        optimization_learnings = [l for l in learnings if l.learning_type == "optimization"]

        lines = ["## Relevant Lessons from Past Runs", ""]

        if failure_learnings:
            for learning in failure_learnings:
                lines.append(f"### {learning.id}: {learning.segment}")
                lines.append(f"- **Failure:** {learning.failure}")
                lines.append(f"- **Root cause:** {learning.root_cause}")
                lines.append(f"- **Fix:** {learning.fix}")
                lines.append("")
            lines.append("Use these lessons to avoid repeating the same mistakes.")
            lines.append("")

        if optimization_learnings:
            lines.append("## Optimized Instructions from Past Runs")
            lines.append("")
            for learning in optimization_learnings:
                lines.append(f"### {learning.id}: {learning.segment} ({learning.improvement_type})")
                lines.append(f"- **For best results:** {learning.prompt_delta}")
                lines.append("")

        return "\n".join(lines)


def _compute_error_signature(error_text: str) -> str:
    first_lines = "\n".join(error_text.strip().splitlines()[:5])
    return hashlib.sha256(first_lines.encode()).hexdigest()[:12]


def _extract_keywords(text: str) -> set[str]:
    words = re.findall(r"[a-zA-Z_][a-zA-Z0-9_]*", text.lower())
    return {w for w in words if len(w) >= 3 and w not in STOP_WORDS}


def _extract_tags(text: str) -> list[str]:
    keywords = _extract_keywords(text)
    tag_indicators = {
        "import", "module", "build", "error", "component", "test", "lint",
        "type", "typescript", "config", "route", "api", "auth", "deploy",
        "missing", "undefined", "null", "timeout", "permission", "syntax",
    }
    return sorted(keywords & tag_indicators)


def _keyword_score(query_keywords: set[str], learning: Learning) -> float:
    learning_text = " ".join([
        learning.failure, learning.root_cause, learning.fix,
        learning.segment, " ".join(learning.tags),
    ]).lower()
    learning_keywords = _extract_keywords(learning_text)
    overlap = query_keywords & learning_keywords
    if not overlap:
        return 0.0
    tag_overlap = query_keywords & set(learning.tags)
    return len(overlap) + len(tag_overlap) * 0.5
