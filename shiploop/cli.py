from __future__ import annotations

import argparse
import asyncio
import json
import logging
import subprocess
import sys
from pathlib import Path

import yaml

from . import __version__
from .config import AGENT_PRESETS, load_config
from .orchestrator import Orchestrator


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="shiploop",
        description=f"Ship Loop v{__version__} — Self-healing build pipeline",
    )
    parser.add_argument(
        "-c", "--config",
        default="SHIPLOOP.yml",
        help="Path to SHIPLOOP.yml (default: ./SHIPLOOP.yml)",
    )
    parser.add_argument(
        "-v", "--verbose",
        action="store_true",
        help="Enable verbose logging",
    )
    parser.add_argument(
        "--version",
        action="version",
        version=f"shiploop {__version__}",
    )

    subparsers = parser.add_subparsers(dest="command", help="Available commands")

    # init
    subparsers.add_parser("init", help="Initialize SHIPLOOP.yml in the current directory")

    # run
    subparsers.add_parser("run", help="Start or resume the pipeline")

    # status
    subparsers.add_parser("status", help="Show current state of all segments")

    # reset
    reset_parser = subparsers.add_parser("reset", help="Reset a segment to pending")
    reset_parser.add_argument("segment", help="Segment name to reset")

    # learnings
    learnings_parser = subparsers.add_parser("learnings", help="Manage learnings")
    learnings_sub = learnings_parser.add_subparsers(dest="learnings_command")
    learnings_sub.add_parser("list", help="Show all learnings")
    search_parser = learnings_sub.add_parser("search", help="Search learnings")
    search_parser.add_argument("query", help="Search query")

    # budget
    subparsers.add_parser("budget", help="Show cost summary")

    args = parser.parse_args(argv)

    _setup_logging(args.verbose)

    if not args.command:
        parser.print_help()
        return 0

    config_path = Path(args.config).resolve()

    try:
        if args.command == "init":
            return _cmd_init()
        elif args.command == "run":
            return _cmd_run(config_path)
        elif args.command == "status":
            return _cmd_status(config_path)
        elif args.command == "reset":
            return _cmd_reset(config_path, args.segment)
        elif args.command == "learnings":
            return _cmd_learnings(config_path, args)
        elif args.command == "budget":
            return _cmd_budget(config_path)
    except FileNotFoundError as e:
        print(f"❌ {e}", file=sys.stderr)
        return 1
    except Exception as e:
        logging.exception("Unexpected error")
        print(f"❌ Unexpected error: {e}", file=sys.stderr)
        return 1

    return 0


def _cmd_init() -> int:
    cwd = Path.cwd()
    config_file = cwd / "SHIPLOOP.yml"

    if config_file.exists():
        print(f"❌ SHIPLOOP.yml already exists in {cwd}", file=sys.stderr)
        return 1

    print(f"🚢 Ship Loop Init — {cwd.name}\n")

    framework = _detect_framework(cwd)
    if framework:
        print(f"  Detected framework: {framework}")

    preflight = _default_preflight(framework)
    deploy_provider = _detect_deploy_provider(cwd)
    git_remote = _detect_git_remote(cwd)

    project_name = input(f"  Project name [{cwd.name}]: ").strip() or cwd.name
    site_url = input("  Site URL (e.g. https://myapp.vercel.app): ").strip()

    print(f"\n  Available agent presets: {', '.join(AGENT_PRESETS)}")
    print("  Or enter a custom command.")
    agent_input = input("  Agent [claude-code]: ").strip() or "claude-code"

    is_preset = agent_input in AGENT_PRESETS
    agent_field = "agent" if is_preset else "agent_command"

    config_data: dict = {
        "project": project_name,
        "repo": str(cwd),
        "site": site_url or "https://example.com",
        agent_field: agent_input,
    }

    if preflight:
        config_data["preflight"] = {k: v for k, v in preflight.items() if v}

    if deploy_provider:
        config_data["deploy"] = {"provider": deploy_provider}

    config_data["segments"] = [
        {
            "name": "example-feature",
            "prompt": "Describe what the agent should build.\n",
        },
    ]

    config_file.write_text(
        yaml.dump(config_data, default_flow_style=False, sort_keys=False, allow_unicode=True)
    )

    print(f"\n✅ Created {config_file}")
    if git_remote:
        print(f"  Git remote: {git_remote}")
    print("  Edit SHIPLOOP.yml to configure your segments, then run: shiploop run")
    return 0


def _detect_framework(cwd: Path) -> str | None:
    markers = {
        "package.json": "node",
        "Cargo.toml": "rust",
        "go.mod": "go",
        "pyproject.toml": "python",
        "setup.py": "python",
    }
    for marker, framework in markers.items():
        if (cwd / marker).exists():
            return framework
    return None


def _default_preflight(framework: str | None) -> dict[str, str | None]:
    presets = {
        "node": {"build": "npm run build", "lint": "npm run lint", "test": "npm test"},
        "python": {"build": None, "lint": "ruff check .", "test": "pytest"},
        "rust": {"build": "cargo build", "lint": "cargo clippy", "test": "cargo test"},
        "go": {"build": "go build ./...", "lint": "golangci-lint run", "test": "go test ./..."},
    }
    return presets.get(framework or "", {"build": None, "lint": None, "test": None})


def _detect_deploy_provider(cwd: Path) -> str | None:
    if (cwd / "vercel.json").exists():
        return "vercel"
    if (cwd / "netlify.toml").exists():
        return "netlify"
    return None


def _detect_git_remote(cwd: Path) -> str | None:
    try:
        result = subprocess.run(
            ["git", "remote", "get-url", "origin"],
            cwd=cwd, capture_output=True, text=True, timeout=5,
        )
        if result.returncode == 0:
            return result.stdout.strip()
    except (subprocess.TimeoutExpired, FileNotFoundError):
        pass
    return None


def _cmd_run(config_path: Path) -> int:
    orchestrator = Orchestrator(config_path)
    success = asyncio.run(orchestrator.run())
    return 0 if success else 1


def _cmd_status(config_path: Path) -> int:
    from .learnings import LearningsEngine

    orchestrator = Orchestrator(config_path)
    statuses = orchestrator.get_status()

    learnings_path = Path(orchestrator.config.repo) / "learnings.yml"
    learnings_engine = LearningsEngine(learnings_path)
    optimization_segments = {
        l.segment for l in learnings_engine.learnings if l.learning_type == "optimization"
    }

    print(f"\n🚢 Ship Loop: {orchestrator.config.project}")
    print("━" * 50)

    status_icons = {
        "pending": "⏳", "coding": "🤖", "preflight": "🛫",
        "shipping": "📦", "verifying": "🔍", "repairing": "🔧",
        "experimenting": "🧪", "shipped": "✅", "failed": "❌",
    }

    for i, seg in enumerate(statuses, 1):
        icon = status_icons.get(seg["status"], "▶")
        deps = f" (depends: {', '.join(seg['depends_on'])})" if seg["depends_on"] else ""
        commit_info = f" [{seg['commit'][:7]}]" if seg.get("commit") else ""
        opt_info = " 🎯 optimized" if seg["name"] in optimization_segments else ""
        print(f"  {i}. {icon} {seg['name']}: {seg['status']}{commit_info}{deps}{opt_info}")

    total = len(statuses)
    shipped = sum(1 for s in statuses if s["status"] == "shipped")
    failed = sum(1 for s in statuses if s["status"] == "failed")
    pending = sum(1 for s in statuses if s["status"] == "pending")

    print(f"\n  {shipped}/{total} shipped, {failed} failed, {pending} pending")
    if optimization_segments:
        print(f"  🔬 Optimizations: {len(optimization_segments)} segment(s)")
    print("━" * 50)
    return 0


def _cmd_reset(config_path: Path, segment_name: str) -> int:
    orchestrator = Orchestrator(config_path)
    if orchestrator.reset_segment(segment_name):
        print(f"✅ Reset '{segment_name}' to pending")
        return 0
    print(f"❌ Segment '{segment_name}' not found", file=sys.stderr)
    return 1


def _cmd_learnings(config_path: Path, args: argparse.Namespace) -> int:
    from .learnings import LearningsEngine

    config = load_config(config_path)
    learnings_path = Path(config.repo) / "learnings.yml"
    engine = LearningsEngine(learnings_path)

    if args.learnings_command == "list":
        if not engine.learnings:
            print("No learnings recorded yet.")
            return 0
        for learning in engine.learnings:
            print(f"\n{learning.id} [{learning.date}] — {learning.segment}")
            print(f"  Failure: {learning.failure}")
            print(f"  Root cause: {learning.root_cause}")
            print(f"  Fix: {learning.fix}")
            if learning.tags:
                print(f"  Tags: {', '.join(learning.tags)}")
        return 0

    elif args.learnings_command == "search":
        results = engine.search(args.query)
        if not results:
            print(f"No learnings matching '{args.query}'")
            return 0
        print(f"Found {len(results)} relevant learning(s):")
        for learning in results:
            print(f"\n{learning.id} [{learning.date}] — {learning.segment}")
            print(f"  Failure: {learning.failure}")
            print(f"  Fix: {learning.fix}")
        return 0

    print("Usage: shiploop learnings [list|search <query>]", file=sys.stderr)
    return 1


def _cmd_budget(config_path: Path) -> int:
    from .budget import BudgetTracker

    config = load_config(config_path)
    metrics_dir = Path(config.repo) / ".shiploop"
    tracker = BudgetTracker(config.budget, metrics_dir)
    summary = tracker.get_summary()

    print(f"\n💰 Budget Summary: {config.project}")
    print("━" * 50)
    print(f"  Total cost:      ${summary['total_cost_usd']:.2f}")
    print(f"  Budget remaining: ${summary['budget_remaining_usd']:.2f}")
    print(f"  Total records:   {summary['total_records']}")

    if summary["by_segment"]:
        print("\n  By segment:")
        for seg, cost in summary["by_segment"].items():
            print(f"    {seg}: ${cost:.2f}")

    print("━" * 50)
    return 0


def _setup_logging(verbose: bool) -> None:
    level = logging.DEBUG if verbose else logging.WARNING
    logging.basicConfig(
        level=level,
        format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
        datefmt="%H:%M:%S",
    )
