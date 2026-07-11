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
