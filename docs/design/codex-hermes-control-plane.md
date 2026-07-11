# Codex Runtime With Hermes Control Plane

Status: revision 4; topic 7351 pilot active. Live verification now covers
policy-bounded startup, runtime-loss reseed, detached routine and important
workers, parent responsiveness, steering, cancellation, completion replay,
and gateway restart with Telegram/cron/Kanban continuity. Broader rollout
remains gated on user-origin governed-memory approval/visibility, cross-topic
isolation, and live rollback exercises.

## Objective

Run every opted-in interactive Telegram topic through a persistent Codex
app-server thread while retaining Hermes as the authoritative shared control
plane for identity, profile policy, durable memory, cross-thread retrieval,
autonomous workers, skills, schedules, notifications, Kanban, and delivery.

The design must improve responsiveness without silently reducing capability.
Topic `7351` remains the only pilot until every rollout gate passes.

## Non-Negotiable Invariants

1. Hermes owns Telegram, session identity, profile selection, durable state,
   cron, notifications, Kanban, and worker lifecycle.
2. Each Telegram topic has a distinct Codex thread. Raw conversation context
   never leaks between topics automatically.
3. `SOUL.md`, profile policy, and bounded durable memory enter Codex through
   trusted protocol fields, never as synthetic user text.
4. Cross-thread history is retrieved explicitly and returned as untrusted data,
   not executable instructions.
5. The foreground parent remains responsive while detached workers run.
6. Worker model/tier is selected by gateway policy. The model cannot promote a
   random task to Sol/xhigh merely by requesting it.
7. Memory writes are validated, bounded, auditable, and optionally staged for
   approval. Worker output and retrieved history cannot write memory directly.
8. Runtime failures are fail-closed. An ambiguous turn is never replayed
   automatically through another runtime.
9. Every durable action has an idempotency key and at-least-once delivery
   semantics. Duplicate delivery must not duplicate the action.
10. Removing one topic's `api_mode` restores its Hermes runtime without
    changing other topics or global services.

## Ownership Model

### Codex owns

- The foreground reasoning/tool loop for one topic.
- Native shell, patch, planning, sandbox, and installed Codex plugins.
- Short-term native thread context and its own internal compaction.

### Hermes owns

- Profile and `SOUL.md` resolution.
- Built-in `USER.md` and `MEMORY.md` stores and external memory providers.
- SQLite transcripts, topic/session bindings, titles, and search.
- Per-topic durable handoff summaries.
- Detached agent dispatch, model policy, budgets, status, steering, cancel,
  completion delivery, and restart reconciliation.
- Skills, cron, schedules, notifications, Kanban, and Telegram delivery.

## Trusted Context Contract

Hermes builds a bounded `CodexControlContext` when starting or resuming a
native thread:

```json
{
  "schema_version": 1,
  "profile": "orchestrator",
  "session_key": "agent:main:telegram:group:-1003931971445:7351",
  "session_id": "...",
  "generation": 4,
  "policy_revision": "sha256:...",
  "stable_policy": "gateway-authored SOUL + profile + operational guidance",
  "memory_revision": "sha256:...",
  "handoff_revision": "sha256:...",
  "capability_scope": ["memory.read", "memory.propose", "sessions.read", "delegate.manage"]
}
```

Only gateway-authored `stable_policy` and non-sensitive revision metadata are
rendered into Codex `developerInstructions` on `thread/start`. USER/MEMORY,
handoffs, transcripts, tool output, and worker output never enter a developer
or system-authority field. They are obtained through scoped tools as
structurally tainted data. Stable policy is immutable for that native thread.
Although Codex 0.144.1 accepts instruction fields on resume, it
may ignore overrides for an already-loaded thread; resume success is therefore
never treated as policy-refresh acknowledgement. Any stable-policy revision
creates a new thread and atomically rebinds it after a verified handoff.
Memory/handoff changes do not force reseed because they are retrieved data, not
thread instructions. The raw transcript is never flattened into this field.

Hermes pins a minimum tested Codex protocol version and validates generated
JSON schemas at startup. Activation fails if `ThreadStartParams` lacks
`developerInstructions`. Integration tests inspect the actual JSON-RPC request
and use an instruction canary on a newly started thread. Canary success is not
used to claim refresh-on-resume support.

Size budgets are configuration-controlled and enforced after rendering:

- stable policy: 24 KiB
- revision metadata: 2 KiB
- total developer instructions: 26 KiB

Overflow fails closed with diagnostics; it is not silently truncated through
an arbitrary byte boundary.

Stable policy tells Codex to call `hermes_context_bootstrap` at the start of a
new native thread when continuity is relevant. That tool returns bounded
USER/MEMORY data, the topic handoff, and active worker handles as untrusted
structured data with revisions and provenance. Failure is explicit and cannot
be mistaken for an empty memory store.

## Control-Plane Transport

The existing `hermes-tools` stdio MCP server remains the Codex-facing tool
surface only after stateful direct dispatch is removed. The gateway provisions
an explicit per-Codex-process MCP entry rather than relying on ambient
`~/.codex/config.toml`. It creates a short-lived capability file for the
specific cached agent:

- mode `0600`, random 256-bit token, stored under the active profile runtime
  directory;
- bound to gateway PID/start time, profile, session key, session ID, generation,
  allowed operations, and expiry;
- token path passed only to that Codex/MCP subprocess environment;
- stores only a token identifier and secret; the gateway stores its hash;
- validated on every stateful call against a Unix-domain gateway control
  socket using token hash, peer UID, audience, method, expiry, profile,
  session, generation, and gateway PID/start time;
- revoked on cache eviction, reset, runtime switch, or gateway shutdown.

The socket is mode `0600` in a mode `0700` runtime directory. Requests and
responses are length-bounded JSON with a schema version, request ID,
idempotency key, method, and typed payload. There is no generic tool-dispatch
method. Read-only tools that are genuinely stateless may continue in-process;
all profile/session-sensitive reads and every write use authenticated RPC. The
MCP child never receives Telegram credentials or arbitrary access to a live
`AIAgent` object. Current direct state-changing Kanban dispatch is removed from
the Codex MCP path and replaced by scoped RPC before this feature can enable.

Capability records have states `active`, `revoked`, and `expired`; validation
is fail-closed if gateway identity changes. Revocation is durable and checked
per request, not inferred from deleting the file. Child environment and Codex
stderr are redacted so token paths/secrets cannot enter transcripts or logs.

Each scoped Codex process receives an exclusive gateway-generated `CODEX_HOME`
under the profile runtime directory. It contains only the authenticated
`hermes-control` MCP entry, explicitly allowlisted read-only MCP servers,
explicitly allowlisted plugins, and a gateway-owned sandbox/approval profile.
Ambient user MCP servers, plugins, instruction files, permission defaults, and
config overrides are excluded. Startup inspects effective configuration and
fails closed on unexpected capabilities, writable roots, network permission,
or permissive profiles. The generated home is removed after child shutdown.
Native project `AGENTS.md` loading is separately policy-controlled and scanned.

## Durable RPC Transaction Model

Hermes adds SQLite control-plane tables owned by the gateway:

- `control_inbox`: principal, profile, session key/id, generation, method,
  idempotency key, canonical request hash, state, lease/fencing token,
  timestamps, result/error.
- `control_outbox`: event ID, inbox ID, destination session, payload hash,
  states `pending|accepted|processed|sent|platform_confirmed`, attempts, and
  timestamps.
- `control_audit`: immutable security and lifecycle events.

The idempotency namespace is `(principal_id, profile, session_id, generation,
method, idempotency_key)`. The same key with a different request hash is a hard
conflict. Concurrent duplicates observe `in_progress` or the stored terminal
result. Deterministic validation failures are cached; transient transport
failures are not. Keys expire only after the maximum action/retry horizon.

Acceptance is committed before execution. Where action and inbox share SQLite,
action state and terminal result commit together. For file-backed memory, the
inbox records a prepared operation and expected memory revision; the locked
memory mutation commits first, then recovery reconciles its exact post-image
hash if result persistence crashes. Outbox acknowledgement always names its
state. The system promises at-least-once delivery with deterministic event-ID
deduplication, not exactly-once Telegram display.

The originating action's terminal result and required outbox row commit in the
same SQLite transaction; an action is not terminal without its outbox record.
`control_outbox.event_id` is a deterministic hash of event type, stable durable
action ID, destination session ID, and canonical payload hash, with a
UNIQUE constraint. Each transition uses compare-and-swap from one legal state;
dispatchers hold expiring leases with fencing tokens. `pending -> accepted` is
owned by the gateway dispatcher, `accepted -> processed` by the Hermes turn
transaction, and later delivery states by the platform adapter. The synthetic
completion message insert and `accepted -> processed` transition occur in one
SQLite transaction with a UNIQUE `(session_id, control_event_id)` constraint.
Recovery retries `pending/accepted` leases; a committed transcript row makes
reprocessing a no-op. A crash after send but before confirmation may duplicate
Telegram display, but cannot create a duplicate Hermes action-layer turn.
Attempt/fencing tokens authorize transitions but are deliberately excluded from
event identity, so lease takeover cannot create a second logical completion.

## Capability Surface

### Profile and context

- `hermes_context_status`: profile, policy revision, memory revision, handoff
  revision, and context budgets; no secret or full-prompt dump.
- `hermes_context_bootstrap`: bounded USER/MEMORY data, current-topic handoff,
  and active worker handles with structural taint and provenance.
- `hermes_thread_handoff_get`: current topic's bounded durable handoff.

### Shared memory

- `hermes_memory_search(query, target, limit)`: bounded read over USER/MEMORY,
  returning entry IDs, revisions, trust class, and provenance metadata.
- `hermes_memory_propose(operations, rationale, idempotency_key)`: passes the
  existing injection scanner, char budget, audit log, and a mandatory
  Codex-origin proposal gate regardless of the general memory-write setting.
  It never bypasses `MemoryStore`.
- No generic memory-file write tool is exposed.

Memory text remains compatible with `USER.md`/`MEMORY.md`, while immutable
provenance lives in SQLite keyed by target, normalized entry hash, and memory
revision. Approval binds the exact operation hash and expected current
revision; stale approval fails. Any proposal derived from session search,
worker output, web/tool output, or another tainted source requires explicit
user approval. Pattern scanning is defense-in-depth, not authorization.

### Cross-thread context

- `hermes_session_search`: a new restricted query path over SessionDB, not a
  wrapper around permissive `session_search()`. Profile is fixed to capability
  scope; cross-profile fallback and full-session reads do not exist. Result
  count, row window, per-field, and total-byte caps are enforced server-side.
- Results carry structural provenance and taint labels through transcript,
  handoff summarization, and memory proposals. They are also enclosed in an
  untrusted-data boundary and labelled historical context, not current truth.
- Full-session dumps are disabled by default for Codex; follow-up scrolling is
  bounded and audited.

### Autonomous workers

- `hermes_delegate_spawn(goal, context, importance, toolsets, role,
  idempotency_key)`
- `hermes_delegate_list(status)`
- `hermes_delegate_status(delegation_id)`
- `hermes_delegate_steer(delegation_id, message, idempotency_key)`
- `hermes_delegate_cancel(delegation_id, reason, idempotency_key)`

Spawn is always detached for an interactive Codex parent. It returns a handle
after durable dispatch and never blocks for completion. This API is not layered
directly over the current process-local async registry. Hermes first adds a
durable delegation state machine:

- delegation row: stable ID, profile/session/generation, goal hash, role,
  policy decision, model/tier, state, attempt, owner lease, fencing token,
  heartbeat, budgets, and terminal result;
- command mailbox: versioned `steer|cancel` commands with sequence number,
  idempotency key, acknowledgement, and expiry;
- attempt row: worker PID/start time or remote execution identity, lease,
  heartbeat, exit classification, and result hash;
- terminal transition uses compare-and-swap on attempt/fencing token so a late
  worker cannot overwrite a newer attempt or cancellation.

Owner death expires the lease and marks the attempt `owner_lost`; it never
claims remote work stopped without evidence. Ambiguous work is not restarted
automatically. Steering is delivered at a worker safe point and acknowledged
with its sequence; unsupported workers report `steering_unsupported`. Targeted
cancellation records intent before signalling and distinguishes
`cancel_requested`, `cancelled`, and `may_still_be_running`.

The durable outbox injects completion into the originating topic. Status,
steering, and cancellation are scoped to delegations created by that
profile/session unless an explicit administrator capability allows broader
control.

The gateway delegation governor maps importance to policy:

- routine: reject delegation or use Terra with a narrow budget;
- important: Sol/xhigh, bounded iterations and toolsets;
- critical/destructive: require explicit user confirmation before dispatch.

The model's `importance` is evidence, not authority. The governor evaluates
task duration, parallelizability, required tools, consequence, current parent
budget, and explicit user wording. Repeated attempts do not increase priority.

## Parent Responsiveness And Steering

Codex turns remain serialized per topic. The foreground Codex turn has a hard
small interactive execution budget. Once work
is dispatched, the parent answers with the worker handle and remains available.
After detached dispatch acknowledgement, new user messages can:

- continue ordinary conversation;
- inspect worker status;
- steer or cancel a named worker;
- interrupt only the active foreground Codex turn.

There is no claim of simultaneous foreground turns. A message arriving during
native tool execution follows explicit gateway policy: use `turn/steer` only
after installed-protocol integration tests; otherwise interrupt and durably
queue the new message. Tests cover arrival before dispatch acknowledgement,
during native tool execution, during approval, and during completion. Detached
workers are not cancelled by an unrelated new message. `/stop`
explicitly interrupts foreground and scoped detached work. Completion delivery
is durable and acknowledged only after the resulting Hermes turn is persisted.

## Context, Compaction, And Reseeding

Four distinct layers are retained:

1. Native Codex thread: recent interactive context and tool history.
2. Hermes transcript: complete searchable audit record, including projected
   Codex events and compaction lineage.
3. Thread handoff: bounded durable summary of decisions, active work, worker
   handles, unresolved questions, and relevant user corrections.
4. Shared USER/MEMORY: stable cross-thread facts and preferences.

Hermes reseeding is authoritative for cross-thread/profile continuity. Codex
native compaction may occur inside a bound thread but is not treated as a
durable Hermes summary or a reseed source. Hermes persists the typed, redacted
protocol events required for reconstruction; the current selective projector
is insufficient and must be extended before reseeding enables. Storage origin
does not imply content trust: user, tool, retrieved-history, plugin, and worker
fields retain taint/provenance through summarization.

Hermes monitors Codex token-usage notifications. At a configurable threshold:

1. create/update the thread handoff from trusted Hermes transcript data;
2. verify the handoff schema and required active-work references;
3. start a new Codex thread with current policy, memory, and handoff;
4. atomically bind the new native thread ID using session generation checks;
5. retain the old native thread ID and Hermes lineage for rollback/audit.

Thread bindings move from session JSON to a gateway-owned SQLite table with
`session_key`, `session_id`, monotonically increasing generation, native thread
ID, expected previous thread ID, policy/memory/handoff revisions, state, and
timestamps. Start/reseed/reset/runtime-switch transitions use compare-and-swap
on generation and expected previous thread. A stale cached agent cannot bind a
thread merely because the Hermes session ID is unchanged.

If summarization fails, keep the old thread and report the failure. Never drop
history or switch bindings merely to satisfy a token threshold. `/reset` starts
a genuinely blank topic conversation but still loads shared profile and memory.

## Persistent Learning

Codex does not autonomously rewrite `SOUL.md`. Learning uses three destinations:

- stable user fact/preference -> governed USER/MEMORY proposal;
- reusable procedure -> skill proposal/review flow;
- topic-local decision/progress -> thread handoff.

Every promotion records source session/message IDs, timestamp, proposing model,
operation, and approval result. Retrieved cross-thread text and worker output
cannot be promoted without the foreground parent restating the candidate and
passing the normal gate.

## Failure Semantics

- Gateway unavailable: stateful MCP calls fail with a retryable error; Codex
  may continue with native read-only tools but cannot claim the action occurred.
- Capability expired/revoked: fail closed and request a fresh foreground turn.
- Duplicate RPC: return the stored result for the idempotency key.
- Worker owner exits: durable ledger reconciles to interrupted or pending
  delivery; completed summaries remain until acknowledged.
- Codex thread missing: preserve Hermes session, build a verified handoff, and
  start a replacement thread. Do not replay the last ambiguous user turn.
- Memory or handoff write fails: retain previous revision atomically.
- Cross-thread search unavailable: state that history retrieval is unavailable;
  never invent continuity.

## Rollback State Machine

Rollback is not merely deleting `api_mode`. Runtime switching is rejected while
a foreground turn is active. For an idle topic, the gateway freezes new worker
dispatch for the active generation, creates and verifies a bounded continuity
handoff containing active worker handles, prepares lineage, then fences the
Codex binding before atomically changing the topic override. If handoff creation
fails, dispatch is unfrozen and the old binding remains active. Capability
validation fails after fencing. Existing workers are not killed: their durable
outbox completions remain routed to the unchanged Hermes session ID and can
therefore re-enter the restored Hermes loop. An ambiguous user turn is never
replayed.

Rollback tests include active native tools, pending approval, running workers,
late worker completion, and a crash between fencing and override persistence.

## Cron, Services, Notifications, And Kanban

The first control-plane rollout exposes read-only status for cron, schedules,
notifications, and Kanban. Mutation is explicitly excluded until each service
has a gateway-owned typed API with destination authorization, approval policy,
idempotency, audit, and secret boundaries. Existing direct Codex-MCP Kanban
mutation is disabled when scoped control-plane mode is active. Background
Hermes services continue independently and are verified by heartbeat/state,
not by model claims.

## Security And Threat Model

Threats include prompt injection in history/worker/tool output, confused-deputy
cross-topic actions, capability theft, stale callbacks after reset, duplicate
side effects after retries, runaway delegation trees, memory poisoning, secret
exfiltration, and context overflow denial of service.

Mitigations:

- trusted policy is gateway-built; retrieved content is always untrusted data;
- session/generation/capability binding on every stateful request;
- allowlisted RPC methods and schemas, no arbitrary tool dispatch;
- pathless memory API and existing content scanner/write approval;
- idempotency table with request hash and durable result;
- depth, concurrency, token, time, and toolset limits enforced server-side;
- no child delegation unless its role/policy explicitly permits it;
- redaction before logs, transcript projection, handoffs, and worker results;
- atomic writes and stale-writer checks;
- audit records for context revisions, searches, memory proposals, and worker
  lifecycle operations.

## Rollout

Phase 0: keep topic 7351 on the current pilot while implementing behind
`gateway.codex_control_plane.enabled: false`.

Phase 1: enable trusted profile context and read-only context status/search for
7351. Verify identity, topic isolation, restart resume, and prompt budgets.

Phase 2: enable governed memory proposals. Verify approval, injection rejection,
atomic limits, provenance, and cross-thread visibility.

Phase 3 (verified 2026-07-11): enable detached spawn/list/status, then steer/cancel. Verify the parent
remains responsive, important-only Sol policy, restart reconciliation, and
duplicate-request idempotency.

Phase 4 (verified through forced runtime-loss reseed): enable handoff compaction/reseed under an artificially low test
threshold, then restore the production threshold after continuity checks.

Phase 5 (verified 2026-07-11): enable
`gateway.codex_control_plane.telegram_topic_default` for foreground Telegram
topics. Exact per-topic `api_mode: codex_responses` overrides remain the
rollback mechanism. This default is consumed only by foreground turn routing;
cron, compression helpers, `/background`, and detached workers retain their
Hermes-owned runtime and policy. Roll out with an explicit topic inventory and
retain one rollback canary rather than bulk-editing individual topic entries.

### Live Evidence (2026-07-11)

- Topic 7351 and topic 2 ran as distinct Codex app-server bindings. Topic 2
  could neither inspect nor cancel topic 7351's worker; mutation failed closed
  with a cross-generation `PermissionError`.
- An approved USER-memory proposal was applied with immutable Telegram actor
  and message provenance, then appeared in a fresh topic bootstrap.
- App-server projections persist one inbound user row with its Telegram
  message ID plus assistant/tool rows. Topic 2 canary message `7475` and topic
  3 default-routing message `7488` verified this contract.
- Topic 2 rollback reached durable `applied` state, fenced generation 3,
  created a verified handoff, revoked the old capability, and restored the
  Responses-backed Hermes loop. The restored loop answered the pre-switch
  continuity canary correctly in 5.6 seconds.
- Rollback testing exposed legacy app-server call IDs longer than the Responses
  limit and dotted MCP function names. Both future projection and legacy
  replay are now deterministically normalized; the actual topic 2 transcript
  passes Responses preflight validation.
- `telegram_topic_default: true` created a fresh active app-server binding for
  topic 3 without an explicit `api_mode`. Telegram remained connected, cron
  heartbeat/last-success advanced after restart, and the embedded Kanban
  dispatcher reacquired its singleton lock.

## Verification Gates

- Profile: Codex can accurately report active profile/policy revision; a SOUL
  canary appears in developer instructions and never in user messages.
- Isolation: topic A cannot retrieve topic B without the bounded search tool and
  cannot act using topic B's capability.
- Memory: approved writes appear across fresh topics; rejected/poisoned writes
  do not; provenance is queryable.
- Delegation: spawn returns promptly, parent answers another message after the
  worker runs, steering changes worker behavior, cancel stops it, and completion
  arrives at least once while deterministic event-ID handling prevents a
  duplicate action-layer turn.
- Model policy: routine tasks cannot force Sol/xhigh; important tasks select it
  with recorded governor rationale.
- Compaction: forced reseed retains policy, memory, decisions, active handles,
  and unresolved work without raw transcript flattening.
- Restart: Codex resumes or safely reseeds; workers reconcile; cron, Telegram,
  notifications, Kanban, and other topics remain operational.
- Failure: ambiguous turns are not replayed; unavailable control-plane actions
  are never reported as successful.
- Rollback: disabling the flag and removing a topic `api_mode` restores the
  original Hermes loop through the rollback state machine.

Fault injection is mandatory at each durable boundary: before/after inbox
acceptance, action commit, result commit, outbox enqueue, Hermes transcript
persist, Telegram send, and acknowledgement. Tests include concurrent duplicate
RPCs with hash conflicts, stale capability/generation, same-session reseed
races, gateway/Codex/MCP/worker process death, semantic memory poisoning without
known scanner phrases, malicious historical content, and rollback during active
work. Assertions inspect persisted state and protocol frames, not only model
responses.

## Initial Implementation Units

1. `CodexControlContext` renderer and protocol injection.
2. Scoped capability lifecycle and local gateway RPC with idempotency storage.
3. Read-only profile/context, memory-search, and session-search MCP tools.
4. Governed memory proposal MCP tool.
5. Durable detached worker spawn/list/status/steer/cancel RPCs.
6. Thread handoff store, token threshold monitor, verified reseed, and lineage.
7. Status/diagnostics, configuration validation, migration, tests, and rollout.
