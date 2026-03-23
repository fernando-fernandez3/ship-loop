# Ship Loop v3.1

Self-healing multi-segment build pipeline with three nested loops, a learnings engine, and budget tracking.

## Quick Start

```bash
pip install pyyaml pydantic
```

Create `SHIPLOOP.yml` in your project, then:

```bash
shiploop run
```

## Architecture

```
┌─────────────── LOOP 1: Ship ───────────────────┐
│  agent → preflight → commit → push → verify    │
│         │                                       │
│      on fail                                    │
│         ▼                                       │
│  ┌──── LOOP 2: Repair ──────┐                  │
│  │  error context → agent   │                  │
│  │  → re-preflight (max N)  │                  │
│  │         │                 │                  │
│  │      exhausted            │                  │
│  │         ▼                 │                  │
│  │  ┌── LOOP 3: Meta ────┐  │                  │
│  │  │  meta-analysis      │  │                  │
│  │  │  → N experiments    │  │                  │
│  │  │  → pick winner      │  │                  │
│  │  └────────────────────┘  │                  │
│  └───────────────────────────┘                  │
│                                                 │
│  📚 Learnings: failures → lessons → future runs │
│  💰 Budget: token/cost tracking per segment     │
└─────────────────────────────────────────────────┘
```

**Loop 1 (Ship):** Code → preflight (build/lint/test) → git commit → push → deploy verify.
Feeds relevant past learnings into the agent prompt.

**Loop 2 (Repair):** On preflight failure, captures error context and asks the agent to fix.
Detects convergence (same error twice) and stops early.

**Loop 3 (Meta):** When repairs stall, runs meta-analysis to identify root cause, spawns N
experiment branches in git worktrees, picks the simplest passing solution.

## CLI Commands

```bash
shiploop run                          # Start or resume pipeline
shiploop status                       # Show segment states
shiploop reset <segment>              # Reset a segment to pending
shiploop learnings list               # List all recorded learnings
shiploop learnings search <query>     # Search learnings by keyword
shiploop budget                       # Show cost summary

# Options
shiploop -c /path/to/SHIPLOOP.yml run # Custom config path
shiploop -v run                       # Verbose logging
shiploop --version                    # Show version
```

## SHIPLOOP.yml

```yaml
project: "My App"
repo: /absolute/path/to/project
site: https://myapp.vercel.app
agent_command: "claude --print --permission-mode bypassPermissions"

preflight:
  build: "npm run build"
  lint: "npm run lint"
  test: "npm test"

deploy:
  provider: vercel          # vercel | netlify | custom
  routes: [/, /api/health]
  timeout: 300

repair:
  max_attempts: 3

meta:
  enabled: true
  experiments: 3

budget:
  max_usd_per_segment: 10.0
  max_usd_per_run: 50.0
  halt_on_breach: true

timeouts:
  agent: 900                # 15 min per agent invocation
  preflight: 300            # 5 min per preflight run
  deploy: 300               # 5 min for deploy verification

segments:
  - name: "dark-mode"
    prompt: |
      Add dark mode with CSS custom properties and a toggle button.
    depends_on: []

  - name: "contact-form"
    prompt: |
      Add contact form at /contact with API endpoint.
    depends_on: [dark-mode]
```

## Project Structure

```
src/
├── cli.py              # CLI interface (argparse)
├── config.py           # SHIPLOOP.yml parsing (Pydantic v2)
├── orchestrator.py     # State machine + DAG scheduler
├── agent.py            # Shared agent runner with timeout enforcement
├── loops/
│   ├── ship.py         # Loop 1: happy path
│   ├── repair.py       # Loop 2: auto-repair
│   └── meta.py         # Loop 3: meta-analysis + experiments
├── preflight.py        # Build/lint/test runner
├── git_ops.py          # Git operations + security scan
├── deploy.py           # Deploy verification plugin loader
├── budget.py           # Token/cost tracking
├── learnings.py        # Failure learning engine
└── reporting.py        # Status output + JSON reports
providers/
├── vercel.py           # Vercel deploy verification
├── netlify.py          # Netlify deploy verification
└── custom.py           # Custom script provider
tests/
├── test_config.py
├── test_orchestrator.py
├── test_git_ops.py
├── test_budget.py
├── test_learnings.py
├── test_repair.py
└── test_meta.py
```

## Documentation

See [SKILL.md](SKILL.md) for full documentation: state machine, execution flow, deploy providers, rollback, crash recovery, and worked examples.

## License

MIT
