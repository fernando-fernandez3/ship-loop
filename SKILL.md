---
name: ship-loop
description: >
  Run a chained build→ship→verify→notify pipeline for multi-segment feature work.
  Use when implementing multiple features in sequence, each as a coding agent task
  that gets committed, deployed, and verified before moving to the next. Prevents
  dropped handoffs between segments.
metadata:
  openclaw:
    emoji: "🚢"
    version: "4.0.0"
    requires:
      bins: ["git", "python3"]
      python: ["pyyaml>=6.0", "pydantic>=2.0"]
    trigger_phrases:
      - "ship loop"
      - "keep building"
      - "run the next segment"
      - "build these features"
      - "multi-feature pipeline"
      - "ship these segments"
---

# Ship Loop

Orchestrate multi-segment feature work as a self-healing pipeline. Three nested loops ensure maximum autonomy: **Loop 1** runs the standard code→preflight→ship→verify chain, **Loop 2** auto-repairs failures via the coding agent, and **Loop 3** spawns experiment branches when repairs stall. A persistent **learnings engine** feeds lessons from past failures into future runs. A **budget tracker** monitors token usage and estimated costs.

## Architecture: Three Nested Loops

```
┌─────────────── LOOP 1: Ship Loop ───────────────┐
│  code → preflight → ship → verify → next        │
│         │                                        │
│      on fail                                     │
│         ▼                                        │
│  ┌──── LOOP 2: Repair Loop ────┐                │
│  │  capture context → agent fix │                │
│  │  → re-preflight (max N)     │                │
│  │         │                    │                │
│  │      exhausted               │                │
│  │         ▼                    │                │
│  │  ┌── LOOP 3: Meta Loop ──┐  │                │
│  │  │  meta-analysis         │  │                │
│  │  │  → N experiment branches│  │                │
│  │  │  → pick winner → merge │  │                │
│  │  └────────────────────────┘  │                │
│  └──────────────────────────────┘                │
│                                                  │
│  📚 Learnings Engine: every failure → lesson     │
│     every run loads relevant lessons into prompt │
│  💰 Budget Tracker: token/cost tracking per run  │
└──────────────────────────────────────────────────┘
```

## Security Notice

> **SHIPLOOP.yml is equivalent to running a script.** The `agent_command`, all preflight commands (`build`, `lint`, `test`), and custom deploy scripts execute with your full user privileges. Ship Loop does **not** sandbox these commands. **Never use on untrusted repos without reviewing the config.** Treat SHIPLOOP.yml with the same caution as a Makefile or CI pipeline.

## When to Use

- Building multiple features for a project in sequence
- Any work that follows: code → preflight → commit → deploy → verify → next
- When you need checkpointing so progress survives session restarts
- When you want self-healing: failures auto-repair before asking humans
- When you want cost visibility across agent invocations

## Prerequisites

- Python 3.10+ with `pyyaml` and `pydantic` installed
- A git repository with a remote
- A deployment pipeline triggered by push (Vercel, Netlify, etc.)
- A coding agent CLI configured via `agent_command` in SHIPLOOP.yml

## Installation

```bash
pip install pyyaml pydantic
```

The CLI lives at `scripts/shiploop` — run directly or add to PATH.

## CLI Usage

```bash
shiploop run              # Start or resume the pipeline
shiploop status           # Show current state of all segments
shiploop reset <segment>  # Reset a segment to pending
shiploop learnings list   # Show all learnings
shiploop learnings search <query>  # Search learnings
shiploop budget           # Show cost summary

# Options
shiploop -c /path/to/SHIPLOOP.yml run   # Custom config path
shiploop -v run                          # Verbose logging
shiploop --version                       # Show version
```

## Pipeline Definition (SHIPLOOP.yml)

### Schema

```yaml
project: "Project Name"
repo: /absolute/path/to/project
site: https://production-url.com
branch: pr               # direct-to-main | per-segment | pr
mode: solo               # solo | team

agent_command: "claude --print --permission-mode bypassPermissions"

preflight:
  build: "npm run build"
  lint: "npm run lint"
  test: "npm run test"

deploy:
  provider: vercel        # vercel | netlify | custom
  routes: [/, /api/health]
  marker: "data-version"
  health_endpoint: /api/health
  deploy_header: x-vercel-deployment-url
  timeout: 300
  script: null            # for custom provider only

repair:
  max_attempts: 3

meta:
  enabled: true
  experiments: 3

budget:
  max_usd_per_segment: 10.0
  max_usd_per_run: 50.0
  max_tokens_per_segment: 500000
  halt_on_breach: true

blocked_patterns:
  - "*.pem"

segments:
  - name: "feature-name"
    status: pending       # pending | coding | preflight | shipping | verifying
                          # | repairing | experimenting | shipped | failed
    prompt: |
      Your coding agent prompt here.
    depends_on: []
    commit: null
    deploy_url: null
    tag: null
    touched_paths: []     # v3.1: for parallel execution detection
```

### Built-in Blocked Patterns

Always rejected regardless of config: `.env`, `.env.*`, `*.key`, `*.pem`, `*.p12`, `*.pfx`, `*.secret`, `id_rsa`, `id_ed25519`, `*.keystore`, `credentials.json`, `service-account*.json`, `token.json`, `.npmrc`, `node_modules/`, `__pycache__/`, `.pytest_cache/`, `*.sqlite`, `*.sqlite3`, `.DS_Store`, `learnings.yml`

## State Machine

```
States per segment:
  pending → coding → preflight → shipping → verifying → shipped
                  ↘ repairing (Loop 2) → preflight
                  ↘ experimenting (Loop 3) → preflight → shipping
                  ↘ failed
```

State is checkpointed to `SHIPLOOP.yml` after every transition. Any crash can be recovered by re-reading the file.

## Execution Flow

### 1. Read SHIPLOOP.yml

Find first segment with `status: pending` whose `depends_on` are all `shipped` (DAG evaluation).

### 2. Run the Segment (Loop 1)

1. Load relevant learnings, prepend to prompt
2. Write prompt to temp file (never shell arguments)
3. Run `agent_command` with prompt via stdin: `cat prompt.txt | {agent_command}`
4. Run preflight (build, lint, test in sequence)
5. If preflight passes → git operations (explicit staging, commit, push, tag)
6. Verify deployment via configured provider
7. Mark shipped

### 3. Repair Loop (Loop 2)

Triggered when preflight fails:

1. Capture error output, failed step, segment state
2. Build REPAIR prompt with full failure context
3. Run agent with repair prompt
4. Re-run preflight
5. If passes → back to ship flow
6. Error signature convergence: if two consecutive attempts produce the same error hash, stop early
7. If max attempts reached → escalate to Loop 3

### 4. Meta Loop (Loop 3)

Triggered when repair exhausts all attempts:

1. Discard uncommitted changes
2. Collect ALL failure context
3. Run agent with META-ANALYSIS prompt
4. Parse experiment descriptions from `---APPROACH N---` markers
5. For each experiment:
   - Create git worktree
   - Run agent with experiment prompt
   - Run preflight
   - Record pass/fail + diff size
6. Pick winner: first passing, simplest diff as tiebreaker
7. Merge winner, clean up experiment branches
8. Continue with ship flow
9. If NO experiments pass → mark segment `failed`

### 5. Chain Continuation

After a segment ships, immediately find and start the next eligible segment.

## Learnings Engine

Every failure-then-fix cycle writes a lesson to `learnings.yml`:

```yaml
- id: L001
  date: "2026-03-23"
  segment: "dark-mode"
  error_signature: "abc123def456"
  failure: "Build failed: Cannot find module './ThemeToggle'"
  root_cause: "Fixed by repair loop attempt 2"
  fix: "Repair agent auto-fixed on attempt 2"
  tags: ["build", "import", "module", "component"]
```

On every run, relevant learnings (matched by keyword scoring against the prompt) are prepended to the agent's prompt.

### CLI Access

```bash
shiploop learnings list
shiploop learnings search "dark mode theme toggle"
```

## Budget Tracking

Token usage and estimated costs are tracked per agent invocation in `.shiploop/metrics.json`.

```bash
shiploop budget
```

Configuration:
- `max_usd_per_segment` — halt if a single segment exceeds this
- `max_usd_per_run` — halt if the entire run exceeds this
- `halt_on_breach` — set `false` to warn but continue

Cost is estimated from token counts parsed from agent output.

## Deploy Verification

### Providers

| Provider | How it works |
|----------|-------------|
| `vercel` | Polls routes for HTTP 200, checks `x-vercel-deployment-url` header |
| `netlify` | Polls routes for HTTP 200, checks `x-nf-request-id` header |
| `custom` | Runs `deploy.script` with `SHIPLOOP_COMMIT` and `SHIPLOOP_SITE` env vars |

## Rollback

Every successful deploy is tagged `shiploop/<segment-name>/<timestamp>`.

```bash
# Revert to last known good
git checkout <last_good_tag> && git push origin HEAD:main --force

# Or revert just the bad commit
git revert <bad_commit> && git push
```

## Crash Recovery

On startup, the orchestrator checks for segments in active states (`coding`, `repairing`, `experimenting`, etc.):
- Active segments are marked `failed`
- A warning is displayed
- The pipeline continues with the next eligible segment

## Critical Rules

1. **Never break the chain** — after a segment ships, immediately start the next
2. **Preflight is mandatory** — no exceptions, no "ship now fix later"
3. **Explicit staging only** — never `git add -A`, only changed files from `git diff`
4. **Prompts via file** — never shell arguments (prevents injection)
5. **Checkpoint everything** — write to SHIPLOOP.yml after every state change
6. **Agent command from config** — always read from `agent_command`, never hardcode
7. **Budget-aware** — track costs, enforce limits, fail gracefully

## Project Structure

```
skills/ship-loop/
├── SKILL.md              # This file
├── scripts/
│   ├── shiploop          # Python CLI entry point (executable)
│   ├── run-segment.sh    # Legacy bash orchestrator
│   ├── preflight.sh      # Legacy bash preflight
│   ├── ship.sh           # Legacy bash ship
│   ├── verify-deploy.sh  # Legacy bash verify
│   ├── repair.sh         # Legacy bash repair
│   ├── meta-experiment.sh # Legacy bash meta
│   └── learnings.sh      # Legacy bash learnings
├── src/
│   ├── __init__.py
│   ├── cli.py            # CLI interface (argparse)
│   ├── config.py         # SHIPLOOP.yml parsing + validation (Pydantic v2)
│   ├── orchestrator.py   # Main state machine + segment runner
│   ├── loops/
│   │   ├── ship.py       # Loop 1: code → preflight → ship → verify
│   │   ├── repair.py     # Loop 2: capture error → agent fix → retry
│   │   └── meta.py       # Loop 3: meta-analysis → experiments → pick winner
│   ├── preflight.py      # Build + lint + test runner
│   ├── git_ops.py        # All git operations (explicit staging, tags, worktrees)
│   ├── deploy.py         # Deploy verification (plugin system)
│   ├── budget.py         # Cost/token tracking + budget enforcement
│   ├── learnings.py      # Learnings engine (record + load + keyword search)
│   └── reporting.py      # Status messages + post-run reports
├── providers/
│   ├── base.py           # Abstract DeployVerifier
│   ├── vercel.py         # Vercel verification
│   ├── netlify.py        # Netlify verification
│   └── custom.py         # Custom script provider
├── requirements.txt
└── tests/
    ├── test_config.py
    ├── test_orchestrator.py
    ├── test_git_ops.py
    └── test_budget.py
```

## Worked Example

### SHIPLOOP.yml

```yaml
project: "Portfolio"
repo: /home/user/portfolio
site: https://portfolio.vercel.app
agent_command: "claude --print --permission-mode bypassPermissions"

repair:
  max_attempts: 3
meta:
  enabled: true
  experiments: 3

preflight:
  build: "npm run build"
  lint: "npx eslint . --max-warnings 0"
  test: "npm test -- --passWithNoTests"

deploy:
  provider: vercel
  routes: [/, /projects]
  deploy_header: x-vercel-deployment-url

budget:
  max_usd_per_segment: 10.0
  max_usd_per_run: 50.0

segments:
  - name: "dark-mode"
    status: pending
    prompt: |
      Add dark mode with CSS custom properties and a toggle button.
    depends_on: []
  - name: "contact-form"
    status: pending
    prompt: |
      Add contact form at /contact with serverless API endpoint.
    depends_on: [dark-mode]
```

### Execution: Happy Path + Repair + Meta

```
🚢 Ship Loop: Portfolio (2 segments)
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

🔄 Segment 1/2: dark-mode
   📚 No prior learnings
   🤖 coding... (0s)
   ✅ Agent completed in 262s
   🛫 preflight... (4m 22s)
   ✅ Preflight passed
   📦 Committed: a1b2c3d
   🏷  Tagged: shiploop/dark-mode/20260323-001500
   ✅ Deploy verified
✅ Segment 1/2: dark-mode — shipped (a1b2c3d) [7m 30s, $0.42]

🔄 Segment 2/2: contact-form
   📚 Loaded 1 relevant learning(s)
   🤖 coding... (0s)
   ✅ Agent completed in 310s
   ❌ Preflight FAILED — entering repair loop
   🔧 Repair attempt 1/3
   ❌ Repair 1 failed: lint errors
   🔧 Repair attempt 2/3
   ❌ Repair 2 failed: test errors
   🔧 Repair attempt 3/3
   ❌ Repair 3 failed: build error
   ❌ Repair loop exhausted (3 attempts)
   🧪 Entering meta loop...
   🧠 Running meta-analysis...
   ✅ Meta-analysis complete
   🧪 Experiment 1/3
   ❌ Experiment 1 failed
   🧪 Experiment 2/3
   ✅ Experiment 2 passed (diff: 12 lines)
   🧪 Experiment 3/3
   ✅ Experiment 3 passed (diff: 18 lines)
   🏆 Winner: experiment 2 (branch: experiment/contact-form-2)
   ✅ Winner merged, experiment branches cleaned
   📦 Committed: e5f6g7h
   🏷  Tagged: shiploop/contact-form/20260323-003200
   ✅ Deploy verified

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
🏁 Ship loop complete! 2/2 segments shipped.
   Total time: 25m 10s
   Total cost: $3.84
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
```

## v3.1 Changes from v3.0

- **Python CLI** replaces bash scripts as the primary interface
- **Pydantic v2** config validation with typed models
- **Budget tracking** — token/cost monitoring with per-segment and per-run limits
- **Enhanced state machine** — explicit states (coding, preflight, shipping, verifying, repairing, experimenting) with checkpoint after every transition
- **DAG-aware scheduling** — parallel segment detection via `touched_paths`
- **Error convergence detection** — hash-based comparison of consecutive repair errors
- **Learnings keyword scoring** — weighted tag/keyword matching for relevant lesson retrieval
- **Deploy provider plugins** — Vercel, Netlify, Custom script with abstract base
- **Crash recovery** — automatic detection and marking of active-state segments on startup
- **Legacy bash scripts preserved** in `scripts/` for reference
