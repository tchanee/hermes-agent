# Autonomous Telegram Orchestrator Implementation Plan

> **For Hermes:** Use subagent-driven-development skill to implement this plan task-by-task.

**Goal:** Build an autonomous Hermes orchestrator that runs from Johnny's main Telegram chat, uses Hermes profiles for role-isolated workers, and uses the native Hermes Kanban system as the durable task board/dispatcher.

**Canonical repo path:** `/Users/tchanee/.hermes/hermes-agent`

**Architecture:** One Telegram-facing orchestrator profile owns the conversation and never lets worker agents speak directly in Telegram. Work is represented as Kanban tasks/runs in a per-board SQLite DB. The dispatcher/autonomous loop claims Ready tasks, spawns headless worker profiles (`architect`, `researcher`, `coder`, `reviewer`) in isolated worktrees where appropriate, and posts event-driven progress updates back to Telegram.

**Tech Stack:** Hermes profiles, Hermes gateway, native Hermes Kanban CLI/dashboard/worker tools, SQLite-backed Kanban DB, existing process/background/gateway notification primitives, git worktrees, role-specific profile config/SOUL.md.

---

## Current State / Discovery

Docs checked on 2026-05-06:

- `https://hermes-agent.nousresearch.com/docs/user-guide/profiles`
- `https://hermes-agent.nousresearch.com/docs/user-guide/features/kanban-tutorial`

Key profile facts from docs:

- A profile is a separate Hermes home directory with its own `config.yaml`, `.env`, `SOUL.md`, sessions, memory, skills, cron jobs, and state.
- Profile aliases are generated automatically, e.g. `coder chat` maps to `hermes -p coder chat`.
- Profiles are state isolation, not filesystem sandboxing.
- `terminal.cwd` must be set explicitly per profile if a predictable project working directory is required.
- Each gateway profile must use a separate platform token; token locks prevent two profiles from using the same Telegram/Discord/Slack/etc. credential.
- Therefore only the orchestrator profile should own Telegram. Worker profiles must be headless/local.

Key Kanban facts from docs:

- Native commands described: `hermes kanban init`, `hermes kanban create`, `hermes kanban show`, `hermes kanban runs`, `hermes dashboard`.
- Dashboard opens at `http://127.0.0.1:9119` and shows Kanban board.
- Worker agents interact through Kanban tools: `kanban_show`, `kanban_complete`, `kanban_block`, `kanban_heartbeat`, `kanban_comment`, `kanban_create`, `kanban_link`.
- Default board DB: `~/.hermes/kanban.db`.
- Multi-board DBs: `~/.hermes/kanban/boards/<slug>/kanban.db`.
- Columns: `Triage`, `Todo`, `Ready`, `In progress`, `Blocked`, `Done`.
- Tasks support assignee, tenant, priority, parent dependencies, run history, summaries, metadata, retries, circuit breaker, crash recovery.
- Gateway hosts an embedded dispatcher that periodically claims Ready tasks and spawns assigned profiles.

Local repo state checked on 2026-05-06:

- Current checkout: `/Users/tchanee/.hermes/hermes-agent` on `main`, local HEAD initially `ff6a86cb` / v0.8.0-era.
- After `git fetch origin --prune`, `origin/main` is `946ef0ea1` and **does contain native Kanban + dashboard code**.
- Current local CLI still does **not** expose `hermes kanban` or the new dashboard until the checkout is updated.
- Native Kanban is in upstream files including `hermes_cli/kanban.py`, `hermes_cli/kanban_db.py`, `tools/kanban_tools.py`, gateway dispatcher/notifier hooks, and prompt-builder Kanban protocol.
- Upstream Kanban DB is intentionally shared across profiles via `kanban_home()` / `get_default_hermes_root()` unless overridden with `HERMES_KANBAN_HOME` or `HERMES_KANBAN_DB`.
- Local working tree has unrelated untracked/modified files, mostly flight/Kalshi artifacts and `package-lock.json`. Do not delete or reset them.
- A normal fast-forward is currently blocked/risky because local `package-lock.json` is modified and upstream also changes `package-lock.json` / `package.json`.
- Running PMB processes checked on 2026-05-06: two Codex workers are active in `/Users/tchanee/Projects/prediction-market-bot`; do not kill/restart them. No `hermes dashboard` process was running. A separate node process is listening on `127.0.0.1:18789` and `127.0.0.1:18791`.

Implication:

- Do **not** reimplement a competing Kanban system. Native Kanban exists upstream.
- First implementation step is to safely update local Hermes to `origin/main` while preserving local dirty files and without touching PMB processes.
- Avoid `hermes update` for this specific update because upstream `hermes update` contains dashboard-stopping behavior. Use explicit git/stash/worktree commands instead.

---

## Target Operating Model

### Agent Sets

Johnny wants one reusable **agent set** per problem/project. A set contains four role agents that stay synchronized through shared memory/state and a shared Kanban board:

- `researcher`
- `designer` (or `architect`; prefer `designer` in user-facing naming if Johnny says designer)
- `coder`
- `reviewer`

There can be multiple sets active at once, e.g.:

- `pmb-researcher`, `pmb-designer`, `pmb-coder`, `pmb-reviewer`
- `hermes-researcher`, `hermes-designer`, `hermes-coder`, `hermes-reviewer`
- `admin-researcher`, `admin-designer`, `admin-coder`, `admin-reviewer`

**Cloning requirement:** every agent in a set should be cloned perfectly from the current profile state at creation time.

Use upstream profile semantics:

- `hermes profile create <name> --clone` copies `config.yaml`, `.env`, `SOUL.md`, installed skills, and curated memory files (`memories/MEMORY.md`, `memories/USER.md`).
- `--clone` is good for initial identity/config cloning, but the copied memories then diverge unless we add a shared-memory mechanism.
- `--clone-all` copies almost all state and strips only runtime files. Use cautiously because it may clone sessions/logs/cache-like state that should not necessarily belong to new workers.

**Shared memory requirement:** the set must not become four isolated brains. Options, in preference order:

1. **Use native shared Kanban as operational memory** for task state, artifacts, run summaries, blockers, and handoffs. Upstream `kanban_db.py` intentionally anchors board DBs at the default Hermes root via `get_default_hermes_root()` / `HERMES_KANBAN_HOME`, so profiles share the same board by design.
2. **Add or configure a shared profile-memory layer** for the curated `MEMORY.md` / `USER.md` files. Current profile cloning copies these files, but does not keep them live-synced. Implement a small sync/link layer only after verifying current upstream memory behavior after update.
3. **Keep role-specific SOUL.md but shared common context.** Each worker should have a role-local SOUL plus a shared set context file injected into prompts, e.g. project path, constraints, safety invariants, Kanban board slug, and coding conventions.

### Orchestrator

One Telegram-facing orchestrator profile owns the conversation. It creates/assigns Kanban work to one or more agent sets. Worker agents must not connect to Johnny's Telegram token.

### Profile naming convention

For each set `<set>`:

1. `<set>-researcher`
   - Headless worker profile.
   - Performs repo/API/docs/web research and writes source-backed notes.
   - No messaging platform token.

2. `<set>-designer`
   - Headless worker profile.
   - Produces design docs, interfaces, schemas, acceptance criteria, and safety invariants.
   - No messaging platform token.

3. `<set>-coder`
   - Headless worker profile.
   - Implements scoped tasks in isolated git worktrees.
   - No messaging platform token.

4. `<set>-reviewer`
   - Headless worker profile.
   - Reviews diffs, tests, docs, and safety invariants.
   - For trading projects: verifies live/prod gates, no fake fills/orders, no market orders, no hot-path regression.
   - No messaging platform token.

### Boards / Tenants

Use one board per major domain or project:

- `default` for general tasks.
- `prediction-market-bot` for `/Users/tchanee/Projects/prediction-market-bot`.
- Later: optional boards for Hermes development, health logging, admin/legal, etc.

Use Kanban `tenant` for repo/project grouping if board-level isolation is not yet sufficient in the installed version.

### Telegram UX

The main Telegram chat becomes the cockpit.

Required slash/on-demand commands:

- `/board` — compact Kanban status by column.
- `/task <id>` — full task details, latest run, blocker, artifacts.
- `/workers` — active claimed tasks/runs and spawned profiles/processes.
- `/blockers` — tasks in Blocked, grouped by required human action.
- `/nudge` — run one dispatcher tick immediately.
- `/approve <id>` — approve a gated next step.
- `/pause <id>` / `/resume <id>` — pause or unblock work where supported.
- `/kill <id>` — terminate worker run if safe.

Required push notifications:

- task created
- task claimed / worker spawned
- heartbeat/progress milestone
- task blocked
- circuit breaker/gave-up
- worker crashed and task reclaimed
- task completed
- reviewer rejected
- reviewer approved
- digest summary

---

## Safety / Correctness Rules

1. Only `orchestrator` may connect to Johnny's Telegram token.
2. Worker profiles must be headless/local and must not send Telegram messages directly.
3. All task state lives in Kanban DB, not chat history.
4. All implementation work that edits code uses isolated git worktrees or an equivalent conflict-safe mechanism.
5. The orchestrator must not recursively spawn unlimited agents; enforce worker concurrency caps.
6. For trading repos, the orchestrator must inject project safety invariants into every worker prompt:
   - Live-SIM is paper-only.
   - Never enable real REST order entry by accident.
   - Never market orders.
   - Never fake fills/PnL.
   - Do not weaken risk gates.
   - Keep market selection/research outside the C++ hot path.
7. A task cannot move to Done until its run summary and metadata include verification results.
8. Reviewer approval is required for tasks that modify production code, gateway behavior, security/token handling, or trading execution/risk logic.

---

## Implementation Phases

### Phase 0: Upgrade / Locate Native Kanban

**Objective:** Bring local Hermes to a version that contains the native Kanban and dashboard features documented publicly.

**Files:**
- Inspect/modify only as needed after update.
- Do not touch unrelated untracked flight/Kalshi artifacts.

**Steps:**

1. Preserve current dirty state summary.

   ```bash
   cd /Users/tchanee/.hermes/hermes-agent
   git status --short
   git branch --show-current
   git log --oneline -5
   ```

2. Fetch upstream and inspect whether Kanban exists on remote.

   ```bash
   git fetch origin
   git branch -r | grep -i kanban || true
   git log --all --oneline --grep='kanban' -20 || true
   git grep -n "kanban_show\|class.*Kanban\|hermes kanban" origin/main || true
   ```

3. If upstream `origin/main` contains Kanban and local is behind, update carefully without discarding local unrelated files.

   ```bash
   git pull --ff-only origin main
   source venv/bin/activate
   python -m pip install -e .
   hermes kanban --help
   hermes dashboard --help
   ```

4. If public docs are ahead of public GitHub, create a local implementation plan for native Kanban first; do not build a separate one-off board.

**Verification:**

- `hermes kanban --help` works.
- `hermes dashboard --help` works.
- `python -m pytest tests/ -q` or a targeted suite passes after update.

---

### Phase 1: Profile Setup for Autonomous Roles

**Objective:** Create role-isolated profiles and configure them for headless worker use.

**Commands:**

```bash
hermes profile create orchestrator --clone
hermes profile create architect --clone
hermes profile create researcher --clone
hermes profile create coder --clone
hermes profile create reviewer --clone
```

Configure worker profiles:

```bash
architect config set terminal.cwd /Users/tchanee
researcher config set terminal.cwd /Users/tchanee
coder config set terminal.cwd /Users/tchanee
reviewer config set terminal.cwd /Users/tchanee
```

Configure `SOUL.md` per role:

- `~/.hermes/profiles/orchestrator/SOUL.md`
- `~/.hermes/profiles/architect/SOUL.md`
- `~/.hermes/profiles/researcher/SOUL.md`
- `~/.hermes/profiles/coder/SOUL.md`
- `~/.hermes/profiles/reviewer/SOUL.md`

Worker `.env` files must not contain Telegram bot tokens unless intentionally configured for a separate bot, which is not the target architecture.

**Verification:**

```bash
hermes profile list
orchestrator profile
architect chat -q "State your role in one sentence."
researcher chat -q "State your role in one sentence."
coder chat -q "State your role in one sentence."
reviewer chat -q "State your role in one sentence."
```

---

### Phase 2: Kanban Board Initialization

**Objective:** Initialize Kanban and define boards/tenants for autonomous work.

**Commands:**

```bash
hermes kanban init
hermes dashboard
```

Create initial PM bot role-pipeline test chain:

```bash
SPEC=$(hermes kanban create "Spec autonomous PM bot live-SIM dashboard work" \
  --assignee architect \
  --tenant prediction-market-bot \
  --priority 2 \
  --body "Write acceptance criteria and constraints for PM bot live-SIM dashboard work. Preserve trading safety invariants." \
  --json | jq -r .id)

IMPL=$(hermes kanban create "Implement autonomous PM bot live-SIM dashboard work" \
  --assignee coder \
  --tenant prediction-market-bot \
  --priority 2 \
  --parent "$SPEC" \
  --body "Implement only after the architect task completes. Use isolated worktree and run tests." \
  --json | jq -r .id)

hermes kanban create "Review autonomous PM bot live-SIM dashboard work" \
  --assignee reviewer \
  --tenant prediction-market-bot \
  --priority 2 \
  --parent "$IMPL" \
  --body "Review diff, tests, docs, and trading safety invariants."
```

**Verification:**

```bash
hermes kanban show "$SPEC"
hermes kanban runs "$SPEC"
```

Dashboard should show the task chain and dependencies.

---

### Phase 3: Telegram Kanban Command Surface

**Objective:** Expose useful Kanban commands inside the Telegram gateway without making users SSH into the box.

**Files likely involved:**

- `hermes_cli/commands.py` — command registry additions if slash commands are central.
- `gateway/run.py` — gateway handlers.
- Kanban CLI/module files once present after upgrade.
- Tests under `tests/gateway/` and/or `tests/kanban/`.

**Commands to add or wire:**

- `/board [tenant|board]`
- `/task <id>`
- `/workers`
- `/blockers`
- `/nudge [board]`
- `/approve <id>`
- `/pause <id>`
- `/resume <id>`
- `/kill <id>`

**Design rules:**

- Gateway command handlers should call Kanban Python APIs directly if available, not shell out to `hermes kanban` unless the internal API is not exposed.
- Responses must be compact and Telegram-readable.
- Long run summaries should be truncated with enough metadata to inspect via `/task <id>`.
- Any dangerous mutation (`/kill`, `/approve` if it triggers side effects) must use existing approval/safety patterns.

**Verification:**

- Unit tests for command parsing.
- Gateway tests for each handler.
- Manual test from Telegram after gateway restart.

---

### Phase 4: Autonomous Dispatcher / Worker Loop

**Objective:** Enable option B autonomy: the system continues checking the board, spawning workers, reclaiming crashed runs, and notifying Telegram without a new user message.

**Likely implementation direction:**

- Prefer native Kanban dispatcher embedded in `hermes gateway start` if present after upgrade.
- Configure dispatcher interval, concurrency limits, failure limits, and enabled profiles.
- If missing, implement only the missing integration layer, not a separate task DB.

**Required behavior:**

- Poll or subscribe to Ready tasks.
- Claim task atomically.
- Spawn assigned profile with `HERMES_KANBAN_TASK=<task_id>`.
- Worker uses `kanban_show` for context.
- Worker emits `kanban_heartbeat`, `kanban_complete`, or `kanban_block`.
- Dispatcher records run outcome.
- Dispatcher promotes child tasks when parents complete.
- Dispatcher detects crash/dead PID and reclaims or blocks after failure limit.

**Worker spawn template:**

```bash
HERMES_KANBAN_TASK=<task_id> \
HERMES_KANBAN_BOARD=<board_slug> \
<assignee-profile> chat -q "You are running a Kanban task. Call kanban_show first, complete the task, heartbeat during long work, and finish with kanban_complete or kanban_block."
```

For code-writing tasks, use `--worktree` or explicit worktree creation.

**Verification:**

- Create a tiny task assigned to `researcher` and watch it auto-complete.
- Create a task that intentionally blocks and verify Telegram notification.
- Kill a worker process and verify crash recovery/retry.
- Verify circuit breaker after repeated spawn failure.

---

### Phase 5: Event-Driven Telegram Notifications

**Objective:** Make the orchestrator proactively tell Johnny when important board events happen.

**Notification triggers:**

- task enters Ready
- task claimed / worker spawned
- heartbeat with material progress
- task blocked
- circuit breaker gave up
- worker crash/retry
- task completed
- reviewer rejected/blocked
- reviewer approved
- daily/periodic digest

**Implementation direction:**

- Prefer Kanban event hooks if present.
- Otherwise poll Kanban event log from the gateway and send only new events to configured home channel.
- Store last-notified event ID per board/profile in Hermes home using `get_hermes_home()`.

**Telegram formatting:**

No markdown assumptions. Keep messages plain-text and compact.

Example:

```text
PMBOT-042 moved: In progress → Review
Coder finished implementation.

Changed:
- ops/dashboard/app.py
- ops/dashboard/state.py
- tests/ops/dashboard/test_state.py

Verification:
- dashboard parser tests: PASS
- live_sim_summary tests: PASS

Reviewer starting now.
```

**Verification:**

- Tests for deduplication.
- Tests for message formatting/truncation.
- Manual Telegram smoke test.

---

### Phase 6: Project Presets / Trading Bot Integration

**Objective:** Make the orchestrator excellent for Johnny's prediction-market bot instead of generic only.

**Create project preset:**

Potential path:

- `~/.hermes/orchestrator/projects/prediction-market-bot.yaml`, or native Kanban project config if upstream has one.

Fields:

```yaml
slug: prediction-market-bot
repo: /Users/tchanee/Projects/prediction-market-bot
board: prediction-market-bot
worker_profiles:
  architect: architect
  researcher: researcher
  coder: coder
  reviewer: reviewer
safety_invariants:
  - Live-SIM is paper-only.
  - Never enable real REST order entry by accident.
  - Never market orders.
  - Never fake fills or fake PnL.
  - Risk breaches must disable entry, cancel, or reduce only.
  - Market investigation/selection stays outside the C++ hot path.
verification_commands:
  - source venv/bin/activate || true
  - cmake --build build --target pmbot_tests paper_trader paper_analytics_report
  - ctest --test-dir build --output-on-failure
```

**Worker prompt injection:**

Every task whose tenant/board is `prediction-market-bot` receives:

- repo path
- safety invariants
- test commands
- coding conventions from memory/project docs
- requirement that design docs/plans precede implementation for trading changes

**Verification:**

- Create PM bot dummy docs-only task and verify architect profile uses project preset.
- Create PM bot implementation task and verify coder receives safety invariants.
- Reviewer must reject a synthetic diff/prompt that weakens a trading gate.

---

## Acceptance Criteria

The feature is considered ready when:

1. `orchestrator` is the only Telegram-connected profile.
2. `architect`, `researcher`, `coder`, and `reviewer` exist as headless profiles.
3. Native Kanban commands and dashboard work locally.
4. Main Telegram chat can show board/task/worker/blocker state.
5. Gateway/autonomous dispatcher can spawn assigned worker profiles without a fresh user message.
6. Worker runs call Kanban tools and record heartbeat/complete/block outcomes.
7. Task dependencies promote correctly from parent completion.
8. Worker crash and spawn failure are recorded and recover/block according to the circuit breaker.
9. Telegram receives event-driven progress updates.
10. For the prediction-market bot, worker prompts include trading safety invariants and isolated worktree requirements.
11. Tests cover command handlers, notification dedupe/formatting, dispatcher behavior, and project preset injection.

---

## Immediate Next Step

Run Phase 0 in a clean, careful way:

1. Fetch upstream.
2. Confirm whether Kanban/dashboard exists in `origin/main` or another upstream branch/tag.
3. Upgrade local Hermes if possible.
4. If native Kanban is still absent from available upstream code, write a separate native-Kanban implementation plan before touching Telegram orchestration.

Do **not** build a parallel one-off board while native Kanban exists in the product direction.
