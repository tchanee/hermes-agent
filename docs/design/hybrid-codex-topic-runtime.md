# Hybrid Codex Topic Runtime

Status: implemented, activated for topic 7351, and live-verified 2026-07-11

## Objective

Run selected interactive Telegram topics through Codex's native app-server
runtime while retaining Hermes as the gateway, scheduler, persistence layer,
notification system and skill library.

The first pilot is Telegram topic `7351` (Bot Management). All other topics,
cron jobs, Kanban workers, and background services remain on their current
Hermes runtime unless configured independently.

## Existing Foundation

Hermes already supports `codex_app_server` as an `api_mode`. It provides:

- Codex-native shell, patching, planning, image viewing, and sandboxing.
- Persistent Codex app-server state for the lifetime of the cached `AIAgent`.
- Hermes gateway delivery, session DB projection, slash commands, accounting,
  memory review, and skill review.
- Native Codex plugins and user MCP servers.
- A `hermes-tools` MCP callback for web, browser, vision, image generation,
  skill inspection, TTS, and Kanban operations.

Two existing runtime gaps must be fixed before the pilot:

- A replacement Codex app-server thread is not seeded from Hermes' stored
  transcript, so restart or cache eviction currently loses conversational
  continuity even though Hermes retains the transcript.
- `AIAgent.interrupt()` does not signal the active
  `CodexAppServerSession`, so a Telegram interruption may leave Codex running
  until its timeout.

The gateway's existing `gateway.session_model_overrides` entries already accept
`api_mode`, making a topic-specific pilot possible without a second Telegram
consumer or a parallel gateway.

## Proposed Configuration

Extend the existing override rather than introduce a second routing system:

```yaml
gateway:
  session_model_overrides:
    agent:main:telegram:group:-1003931971445:7351:
      model: gpt-5.6-terra
      provider: openai-codex
      api_mode: codex_app_server
      reasoning_effort: medium
```

All shown fields already exist. Configuration validation must explicitly allow
`codex_app_server` as a per-session `api_mode`; no new configuration fields are
required for the pilot.

The global `model.openai_runtime` remains `auto`, ensuring cron and unrelated
Hermes sessions do not move to Codex accidentally.

## Request Path

1. The Telegram adapter receives a message normally.
2. The gateway derives the existing session key, including the topic ID.
3. `_resolve_session_agent_runtime` applies the configured per-session
   `api_mode: codex_app_server`.
4. The normal gateway pipeline builds or reuses the topic's cached `AIAgent`.
5. `AIAgent.run_conversation` hands the turn to its `CodexAppServerSession`.
6. Codex executes native tools and may call Hermes tools through MCP.
7. Projected Codex events are stored in Hermes' session DB.
8. Hermes formats and sends the final answer through the existing Telegram
   adapter.

No Telegram polling, cron scheduling, notification delivery, or channel
authorization code is duplicated.

## Hermes Capabilities During The Pilot

Codex already receives Hermes web, browser, vision, image, skill-inspection,
TTS, and Kanban tools through the existing `hermes-tools` MCP server. Reuse
those tools rather than introduce a second broker.

The following Hermes agent-loop tools remain unavailable in the pilot:
`delegate_task`, direct `memory`, `session_search`, and persistent Hermes
`todo`. Existing Kanban operations provide durable task dispatch/status, and
Codex's `update_plan` covers turn-local planning. Hermes' background memory and
skill review remains active through projected Codex events.

Do not expose cron writes, memory writes, or arbitrary Hermes tool invocation
during the pilot. If schedule visibility is required, add only a structured,
bounded, read-only `cron_list` tool to the existing `hermes-tools` allowlist.
Any later write bridge requires a gateway-authenticated request transport and a
transactional idempotency design; a token placed in a Codex child process is
not an adequate security boundary.

## Cron, Schedules, and Notifications

Cron remains a gateway subsystem and is independent of the interactive topic's
runtime. Existing jobs continue to tick, execute, and deliver through Hermes.

The pilot does not change:

- `cron/jobs.json` or scheduler locking.
- Existing cron job runtime/model/profile choices.
- Telegram delivery routing or topic metadata.
- Kanban dispatcher/notifier loops.
- Background process and delegation completion queues.

Codex does not manage schedules during the initial pilot. The gateway remains
the scheduler, so a gateway restart does not lose schedule definitions.

## Skills and Tools

Codex gets three capability layers:

1. Codex built-ins: shell, patch, plan, image, and web search.
2. Native Codex plugins and configured MCP servers.
3. Existing Hermes MCP tools.

Hermes skills remain available through `skills_list` and `skill_view`. Codex
loads a relevant skill on demand instead of injecting the complete Hermes skill
index into every turn. Work requiring unavailable Hermes agent-loop tools stays
on a Hermes topic or uses existing Kanban operations.

## Session and Restart Semantics

- Hermes session ID remains the public durable conversation identity.
- Codex thread/process state is an implementation detail of the cached agent.
- Persist the native Codex thread ID in Hermes' `SessionEntry` and resume it
  through app-server `thread/resume` after cache eviction or gateway restart.
  This preserves native role and tool history without flattening trusted and
  untrusted transcript content into one synthetic user message.
- `/reset` creates a fresh Hermes `SessionEntry`, intentionally dropping the
  old Codex thread binding. Compression updates the existing entry and retains
  the binding.
- An active Codex turn is interrupted on gateway shutdown. Existing detached
  Hermes/Kanban work retains its own durable status and reconciliation.
- `/status` must report live gateway, Codex subprocess, and detached
  Hermes/Kanban task state.
  It must never answer `running` solely from transcript text.
- `/reset` clears the Hermes topic session and retires its Codex subprocess.

## Failure and Runtime-Switch Policy

Automatic cross-runtime replay is not permitted:

1. If startup, thread binding, or `turn/start` fails, report the failure and
   retire the Codex session. A timeout cannot prove that no side effect began.
2. After `turn/start` succeeds, any timeout, disconnect, or unknown outcome is
   also non-replayable. Return a partial/interrupted result and require a status
   check or a new user turn.
3. Hermes-default runtime remains reachable through a topic-scoped command,
   `/runtime hermes`, and Codex can be restored with `/runtime codex`.
4. A circuit breaker is deferred until the pilot demonstrates repeated startup
   failure; it is not required for initial activation.

## Concurrency

- Preserve the gateway's existing one-active-turn-per-session behavior.
- Add a direct bridge from `AIAgent.interrupt()` to
  `CodexAppServerSession.interrupt()`. A new Telegram message can then interrupt
  the active Codex turn but does not cancel detached Hermes/Kanban tasks.
- Completion events re-enter the originating topic through the existing queue.

## Observability

Add structured events for:

- selected runtime and reason;
- Codex startup, thread ID, turn ID, duration, and exit classification;
- startup/turn-start failure classification and session retirement;
- thread start/resume decision and interruption outcome;
- restart reconciliation.

`/status` should summarize these live records without exposing credentials or
full prompts.

## Rollout

1. Snapshot Hermes config, Codex config, and the topic session mapping.
2. Run the existing Codex runtime migration without changing the global
   runtime; verify Codex auth, managed MCP block, and `hermes-tools` startup.
3. Add `api_mode: codex_app_server` only to topic `7351`.
4. Exercise plain chat, repository inspection, a safe patch, skill lookup,
   existing Kanban visibility, restart recovery, interruption, and Telegram
   delivery.
5. Keep all other topics on Hermes for at least several days of observation.

## Verification Matrix

- Routing: only topic `7351` selects Codex.
- Isolation: General and trading topics retain their current models/runtimes.
- Cron: existing jobs tick and deliver while Codex topic is active.
- Tools: Codex built-ins, native plugins, existing Hermes MCP and Kanban tools.
- Restart: active Codex turns report interrupted, and detached Hermes/Kanban
  task status comes from its durable store rather than chat history.
- Failure: startup and turn-start failures are reported without replay.
- Security: no new sensitive write surface is introduced by the pilot.
- Session: `/reset` starts fresh; cache eviction, compression, and restart
  preserve native Codex continuity through `thread/resume`.
- Rollback: removing `api_mode` from the one override restores Hermes without
  altering cron, jobs, sessions, or Telegram configuration.

## Non-Goals

- Replacing the Telegram gateway or running a second bot poller.
- Moving cron scheduling into Codex.
- Globally enabling Codex for every Hermes profile or worker.
- Exposing arbitrary Hermes tool execution through MCP.
- Guaranteeing continuation of an in-flight Codex process across restart.

## Implementation Units

1. Per-topic runtime configuration validation and topic-scoped runtime command.
2. Durable Codex thread-ID persistence and native `thread/resume`.
3. `AIAgent.interrupt()` to Codex `turn/interrupt` bridge.
4. Fail-closed startup and `turn/start` classification.
5. Topic-scoped `/runtime hermes|codex|status` command that changes only the
   current session override and evicts only that cached agent.
6. Live status aggregation and restart handling.
7. Focused unit, integration, and Telegram pilot tests.

## Architecture Review

An independent code-based review was completed after the initial draft. Its
accepted findings drove this revision:

- Critical: replacement Codex threads lacked continuity. Native
  `thread/resume` was selected after validation against the installed Codex
  app-server, avoiding unsafe flattened transcript replay.
- High: gateway interruption was not connected to Codex `turn/interrupt`.
- High: the proposed capability-token model did not match the stdio child
  topology and was not a meaningful security boundary.
- High: automatic replay across runtimes can duplicate side effects because a
  timeout does not prove rejection. Runtime changes must be explicit.
- Medium: the proposed broker duplicated existing Kanban and cron services and
  introduced undefined idempotency semantics.
- Medium: the draft used unsupported configuration fields and referenced the
  wrong runtime resolver name.
- Medium: the existing `/codex-runtime` command is global and cannot be reused
  as a topic-local switch.

The revised recommendation is therefore a small, read-only-first pilot built
on existing per-topic `api_mode`, existing Hermes MCP/Kanban tools, transcript
thread resumption, and correct interruption. Capability expansion is explicitly deferred
until demonstrated need and a defensible authorization boundary exist.
