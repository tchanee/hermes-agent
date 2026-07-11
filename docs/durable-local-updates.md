# Durable Updates For This Hermes Installation

This checkout contains a locally maintained Codex Telegram control plane. The
local commits are part of the installation, not disposable working-tree edits.

Do not resolve an update divergence with `git reset --hard origin/main`.
`hermes update` now refuses that operation when local-only commits exist.

Use the transactional updater instead:

```bash
cd ~/.hermes/hermes-agent
scripts/update-preserving-local-commits.sh
scripts/update-preserving-local-commits.sh --apply
```

The first command is a dry run. It fetches official `origin/main`, merges it
into the preserved local lineage in a temporary worktree, checks required control-plane surfaces,
and runs the focused regression suite. It never changes the live checkout or
gateway.

`--apply` repeats the same gate, creates `backup/pre-update-<timestamp>`, moves
the live `main` branch only to the tested candidate, validates the orchestrator
configuration, and restarts the gateway. A failed integration or test leaves
the live installation unchanged. A failed post-activation config check restores
the backup branch immediately.

The profile configuration, memory, sessions, control-plane database, cron jobs,
and Kanban state live under `~/.hermes/profiles/orchestrator` and are not stored
in the source checkout. Hermes's normal pre-update state snapshot remains an
additional recovery layer.

## Detached Worker Runtime

As of 2026-07-12, general detached workers run through the Codex app-server
runtime while Hermes remains the gateway and lifecycle control plane. The
gateway, not model text, selects the runtime. Requests using the Hermes-native
`cronjob`, `kanban`, or `skills` toolsets stay on the Hermes loop; general
terminal, file, web, research, and coding work defaults to Codex.

Runtime selection is durable in `control_delegations.worker_runtime` and is
included in worker status, handoffs, completion events, and audit records.
Importance is independent: routine workers use Terra; only gateway-validated
consequential work uses Sol/xhigh. Existing database rows migrate
conservatively to `hermes`; only newly governed dispatches default to Codex.

After an update, verify both paths:

```bash
cd ~/.hermes/hermes-agent
uv run --extra dev --extra messaging pytest \
  tests/gateway/test_codex_control_store.py \
  tests/gateway/test_codex_delegation_service.py \
  tests/gateway/test_codex_handoff_service.py \
  tests/tools/test_delegate.py -q
scripts/hermes-gateway restart
scripts/hermes-gateway status
```

Then dispatch one harmless general worker and one harmless worker requiring a
Hermes-native toolset. Confirm `worker_runtime` is respectively `codex` and
`hermes`, completion returns to the originating topic, and the foreground topic
answers another message while either worker is active. To roll back worker
execution without changing foreground topics, revert the worker-runtime commit
and restart the gateway. Existing workers and durable handles are not deleted.

## Sol Safety Policy

GPT-5.6 Sol workers must not receive prompts that reward sustained persistence.
Generated worker instructions enforce named-target scope, prohibit substituting
resources or broadening cleanup, prohibit discovering or moving credentials
outside the user's explicit authorization, and require blockers at destructive,
remote-mutation, security-control, and credential boundaries. Completion claims
must be backed by tool output or another verifiable artifact and must distinguish
partial or unperformed work.

The orchestrator profile runs important Sol workers at `high`, not `xhigh`,
reasoning effort. Before presenting consequential claims or authorizing
follow-on mutations, the foreground agent must independently review the
worker's evidence. This is Hermes policy enforcement; do not describe it as a
Codex `auto-review` setting unless the installed Codex release exposes and
verifies such a setting.
