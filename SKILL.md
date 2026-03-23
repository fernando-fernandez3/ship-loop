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
    version: "3.0.0"
    requires:
      bins: ["git", "bash", "curl"]
    trigger_phrases:
      - "ship loop"
      - "keep building"
      - "run the next segment"
      - "build these features"
      - "multi-feature pipeline"
      - "ship these segments"
---

# Ship Loop

Orchestrate multi-segment feature work as a self-healing pipeline. Three nested loops ensure maximum autonomy: **Loop 1** runs the standard code→preflight→ship→verify chain, **Loop 2** auto-repairs failures via the coding agent, and **Loop 3** spawns experiment branches when repairs stall. A persistent **learnings engine** feeds lessons from past failures into future runs.

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
└──────────────────────────────────────────────────┘
```

## When to Use

- Building multiple features for a project in sequence
- Any work that follows: code → preflight → commit → deploy → verify → next
- When you need checkpointing so progress survives session restarts
- When you want self-healing: failures auto-repair before asking humans

## Prerequisites

- A git repository with a remote
- A deployment pipeline triggered by push (Vercel, Netlify, etc.)
- A coding agent CLI configured via `agent_command` in SHIPLOOP.yml

## Pipeline Definition (SHIPLOOP.yml)

### Schema

```yaml
project: "Project Name"
repo: /absolute/path/to/project
site: https://production-url.com
platform: vercel         # vercel | netlify | static | custom
branch: direct-to-main  # direct-to-main | per-segment

agent_command: "claude --print --permission-mode bypassPermissions"

verify:
  routes: [/, /api/health]
  marker: "data-version"
  health_endpoint: /api/health
  deploy_header: x-vercel-deployment-url

timeouts:
  agent: 900
  deploy: 300
  verify: 180

preflight:
  build: "npm run build"
  lint: "npm run lint"
  test: "npm run test"

# Loop 2: Repair config
repair:
  max_attempts: 3        # Max repair attempts before escalating to meta loop

# Loop 3: Meta loop config
meta:
  enabled: true          # Set false to skip meta loop (fail immediately after repair)
  experiments: 3         # Number of experiment branches to try

blocked_patterns:
  - "*.pem"

rollback:
  last_good_commit: null
  last_good_tag: null

segments:
  - name: "feature-name"
    status: pending      # pending | running | shipped | failed
    prompt: |
      Your coding agent prompt here.
    depends_on: []
    commit: null
    deploy_url: null
    tag: null
```

### Built-in Blocked Patterns

Always rejected regardless of config: `.env`, `*.key`, `*.pem`, `*.p12`, `*.secret`, `id_rsa`, `id_ed25519`, `credentials.json`, `service-account*.json`, `token.json`, `node_modules/`, `__pycache__/`

## Scripts

This skill includes 7 scripts in `scripts/`. Copy to your project:

```bash
cp scripts/*.sh <repo>/scripts/ && chmod +x <repo>/scripts/*.sh
```

| Script | Purpose |
|--------|---------|
| `run-segment.sh` | Main orchestrator: runs all 3 loops for a segment |
| `preflight.sh` | Build + lint + test gate |
| `ship.sh` | Stage, security scan, commit, push, tag, verify |
| `verify-deploy.sh` | Poll production for successful deployment |
| `repair.sh` | Loop 2: construct repair prompt, run agent |
| `meta-experiment.sh` | Loop 3: run a single experiment branch |
| `learnings.sh` | Record and load persistent learnings |

## Execution Flow

### 1. Read SHIPLOOP.yml

Find first segment with `status: pending` whose `depends_on` are all `shipped`.

### 2. Run the Segment

```bash
PROMPT_FILE=$(mktemp /tmp/shiploop-prompt-XXXXXX.txt)
cat > "$PROMPT_FILE" << 'PROMPT_EOF'
<segment prompt>
PROMPT_EOF

cd <repo> && bash scripts/run-segment.sh "<segment-name>" "$PROMPT_FILE"
```

Run as background exec, poll with `process(action="poll", sessionId="...", timeout=30000)`.

### 3. What Happens Inside run-segment.sh

**Learnings injection**: If `learnings.yml` exists, relevant past lessons are prepended to the prompt.

**Loop 1** (standard):
1. Run coding agent with prompt via stdin
2. Run preflight (build + lint + test)
3. If preflight passes → ship (commit, push, verify)

**Loop 2** (on preflight failure):
1. Capture error output + git diff + segment state
2. Call `repair.sh` with failure context
3. Agent receives a REPAIR prompt describing what failed
4. Re-run preflight
5. Repeat up to `repair.max_attempts` (default 3)
6. Track error signatures for convergence detection

**Loop 3** (when repair exhausts):
1. Collect ALL failure context (every attempt, every error, every diff)
2. Run agent with META-ANALYSIS prompt: "Why do all approaches fail?"
3. Agent generates N experiment prompts, each with a different approach
4. For each experiment:
   - Create branch: `experiment/<segment-name>-<N>`
   - Run agent with experiment prompt
   - Run preflight
   - If passes → record as candidate
5. Pick winner: first passing experiment (simplest diff as tiebreaker)
6. Merge winner to main branch, continue ship flow
7. If NO winner → segment `failed`, surface all results to user

**After any fix**: Record lesson to `learnings.yml`.

### 4. Check Results & Continue

- **Exit 0**: Update SHIPLOOP.yml (`status: shipped`, record commit/tag/url), start next segment
- **Non-zero**: Set `status: failed`, report to user with rollback info

### 5. Chain Continuation

After a segment ships, immediately find and start the next eligible segment. Don't wait for user.

## Learnings Engine

Every failure-then-fix cycle writes a lesson to `learnings.yml` in the project root:

```yaml
- id: L001
  date: "2026-03-23"
  segment: "dark-mode"
  failure: "Build failed: Cannot find module './ThemeToggle'"
  root_cause: "Fixed by repair loop attempt 2"
  fix: "Repair agent auto-fixed the issue"
  applies_to: [prompt, code]

- id: L002
  date: "2026-03-23"
  segment: "auth-flow"
  failure: "Repair loop exhausted after 3 attempts"
  root_cause: "Required meta-analysis and experiment branching"
  fix: "Experiment 2 succeeded with alternative approach"
  applies_to: [prompt, code]
```

On every run, relevant learnings (matched by prompt keywords) are prepended to the agent's prompt. Over time, the pipeline encounters fewer novel failures.

### Manual Commands

```bash
# Record a learning manually
bash scripts/learnings.sh record "segment" "what failed" "why" "how to avoid"

# Load relevant learnings for context
bash scripts/learnings.sh load "dark mode theme toggle"
```

## Rollback

Every successful deploy is tagged `shiploop/<segment-name>/<timestamp>`.

```bash
# Revert to last known good
git checkout <last_good_tag> && git push origin HEAD:main --force

# Or revert just the bad commit
git revert <bad_commit> && git push
```

## Branch Strategy

- **`direct-to-main`** (default): All commits go straight to main. Fast, rollback via tags.
- **`per-segment`**: Each segment gets branch `shiploop/<segment-name>`, merged after verify.

## Crash Recovery

On session start, check SHIPLOOP.yml for `status: running` segments:
1. Check if process is still alive via `process(action="list")`
2. If running: resume polling
3. If not found: mark `failed`, report to user (do NOT auto-retry)

## Platform Verification

- **Vercel**: Check `x-vercel-deployment-url` header for new deployment
- **Netlify**: Check `x-nf-request-id`, poll deploy API for `ready` status
- **Static/GitHub Pages**: HTTP 200 + marker check
- **Custom**: Override `verify-deploy.sh`

## Critical Rules

1. **Never break the chain** — after a segment ships, immediately start the next
2. **Preflight is mandatory** — no exceptions, no "ship now fix later"
3. **Explicit staging only** — never `git add -A`, only changed files from `git diff`
4. **Prompts via file** — never shell arguments (prevents injection)
5. **Checkpoint everything** — write to SHIPLOOP.yml after every state change
6. **Agent command from config** — always read from `agent_command`, never hardcode

## Worked Example

### SHIPLOOP.yml

```yaml
project: "Portfolio"
repo: /home/user/portfolio
site: https://portfolio.vercel.app
platform: vercel
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

verify:
  routes: [/, /projects]
  deploy_header: x-vercel-deployment-url

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
🚢 Ship loop started: Portfolio (2 segments)

🔄 Segment 1/2: dark-mode
   📚 No prior learnings
   🤖 Agent completed in 4m 22s
   ✅ Preflight passed
   📦 Committed: a1b2c3d
   🏷 Tagged: shiploop/dark-mode/20260323-001500
   ✅ Deploy verified
✅ Segment 1/2: dark-mode — shipped

🔄 Segment 2/2: contact-form
   📚 Loaded 1 relevant learning
   🤖 Agent completed in 5m 10s
   ❌ Preflight FAILED — lint errors

   🔧 Repair attempt 1/3
      Agent fixed unused imports
      ❌ Preflight still failing — test errors

   🔧 Repair attempt 2/3
      Agent fixed test mocking
      ❌ Preflight still failing — build error

   🔧 Repair attempt 3/3
      Agent restructured component
      ❌ Preflight still failing

   ❌ Repair loop exhausted (3 attempts)

   🧪 Entering meta loop...
      🧠 Meta-analysis: "Form component uses server action syntax
         incompatible with current Next.js config"
      🧪 Experiment 1/3: API route approach → ❌ failed
      🧪 Experiment 2/3: Client-side fetch → ✅ passed (12 lines diff)
      🧪 Experiment 3/3: Server action with config fix → ✅ passed (18 lines diff)
      🏆 Winner: experiment 2 (simplest diff)
      Merged to main

   📦 Committed: e5f6g7h
   🏷 Tagged: shiploop/contact-form/20260323-003200
   ✅ Deploy verified
   📝 Learning L002 recorded
✅ Segment 2/2: contact-form — shipped

🏁 Ship loop complete! 2/2 segments shipped.
```

### Pipeline Improvement Over Time

```
Night 1:  5 segments, 3 first-try, 1 repair-fixed, 1 meta-fixed  (6 hrs)
Night 10: 5 segments, 4 first-try, 1 repair-fixed                (2 hrs)
Night 50: 5 segments, 5 first-try                                 (1.5 hrs)
```

The learnings engine front-loads past lessons into prompts, so the agent avoids known failure patterns.

## Status Messages

After each segment:
```
✅ Segment 3/7: Dark mode — shipped (a1b2c3d)
🔄 Starting Segment 4/7: Trip management...
```

On failure (all loops exhausted):
```
❌ Segment 4/7: Trip management — FAILED
   Repair: 3/3 attempts failed
   Meta: 3/3 experiments failed
   Last good: shiploop/dark-mode/20260323-001500 (a1b2c3d)
   Failure history: /tmp/shiploop-failure-history-*.txt
```

## Starting a Ship Loop

1. Create `SHIPLOOP.yml` with all segments defined
2. Copy scripts from this skill's `scripts/` directory
3. Confirm plan with user
4. Start with first eligible segment

## Resuming a Ship Loop

1. Read `SHIPLOOP.yml`
2. Report status of all segments
3. Run crash recovery for any `running` segments
4. Continue with next `pending` segment
