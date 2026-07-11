#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
APPLY=0
if [[ "${1:-}" == "--apply" ]]; then
  APPLY=1
elif [[ $# -gt 0 ]]; then
  echo "usage: $0 [--apply]" >&2
  exit 2
fi

cd "$ROOT"
if [[ -n "$(git status --porcelain --untracked-files=no)" ]]; then
  echo "refusing update: tracked worktree changes are present" >&2
  exit 2
fi
if [[ "$(git branch --show-current)" != "main" ]]; then
  echo "refusing update: expected the live checkout on branch main" >&2
  exit 2
fi

echo "fetching official upstream main"
git fetch origin main

old_head="$(git rev-parse HEAD)"
upstream="$(git rev-parse origin/main)"
base="$(git merge-base "$old_head" "$upstream")"
local_count="$(git rev-list --count "$base..$old_head")"
upstream_count="$(git rev-list --count "$base..$upstream")"

if [[ "$upstream_count" == "0" ]]; then
  echo "already based on the current origin/main"
  exit 0
fi

stamp="$(date -u +%Y%m%dT%H%M%SZ)"
candidate_branch="update-candidate-$stamp"
candidate_dir="$(mktemp -d "${TMPDIR:-/tmp}/hermes-update.XXXXXX")"

cleanup() {
  git worktree remove --force "$candidate_dir" >/dev/null 2>&1 || true
  git branch -D "$candidate_branch" >/dev/null 2>&1 || true
}
trap cleanup EXIT

echo "integrating $upstream_count upstream commit(s) with $local_count preserved local commit(s)"
git worktree add -q -b "$candidate_branch" "$candidate_dir" "$old_head"
if ! git -C "$candidate_dir" merge --no-edit --no-ff "$upstream"; then
  git -C "$candidate_dir" merge --abort >/dev/null 2>&1 || true
  echo "candidate integration conflicted; live checkout is unchanged" >&2
  exit 1
fi

required=(
  "gateway/codex_control.py"
  "agent/codex_control_context.py"
  "agent/codex_runtime.py"
  "hermes_cli/codex_runtime_switch.py"
  "docs/design/codex-hermes-control-plane.md"
)
for path in "${required[@]}"; do
  if [[ ! -f "$candidate_dir/$path" ]]; then
    echo "candidate lost required control-plane file: $path" >&2
    exit 1
  fi
done

rg -q 'telegram_topic_default' "$candidate_dir/gateway/run.py"
rg -q 'hermes_worker_spawn' "$candidate_dir/gateway/codex_control.py"
rg -q 'runtime_transitions' "$candidate_dir/gateway/codex_control.py" \
  "$candidate_dir/gateway/codex_control_store.py"

python_bin="$ROOT/venv/bin/python"
if [[ ! -x "$python_bin" ]]; then
  echo "missing Hermes virtualenv Python: $python_bin" >&2
  exit 1
fi

tests=(
  tests/agent/test_codex_control_context.py
  tests/agent/transports/test_codex_event_projector.py
  tests/gateway/test_codex_context_service.py
  tests/gateway/test_codex_control_rpc.py
  tests/gateway/test_codex_control_runtime.py
  tests/gateway/test_codex_control_store.py
  tests/gateway/test_codex_delegation_service.py
  tests/gateway/test_codex_handoff_service.py
  tests/gateway/test_codex_memory_service.py
  tests/gateway/test_codex_services_service.py
  tests/gateway/test_session_model_override_routing.py
  tests/gateway/test_topic_runtime_command.py
  tests/run_agent/test_codex_app_server_integration.py
  tests/run_agent/test_identity_flush.py
  tests/run_agent/test_run_agent_codex_responses.py
)

echo "running control-plane regression gate in isolated worktree"
(
  cd "$candidate_dir"
  PYTHONPATH="$candidate_dir" "$python_bin" -m pytest -q "${tests[@]}"
)

candidate_head="$(git -C "$candidate_dir" rev-parse HEAD)"
echo "candidate passed: $candidate_head"
if [[ "$APPLY" == "0" ]]; then
  echo "dry run complete; live checkout and gateway were not changed"
  echo "run $0 --apply to activate this tested candidate"
  exit 0
fi

backup_branch="backup/pre-update-$stamp"
git branch "$backup_branch" "$old_head"
echo "backup branch created: $backup_branch"

git merge --ff-only "$candidate_head"
if ! "$ROOT/venv/bin/hermes" --profile orchestrator config check >/dev/null; then
  echo "post-activation config check failed; restoring $backup_branch" >&2
  git reset --hard "$backup_branch"
  exit 1
fi

pid_file="$HOME/.hermes/profiles/orchestrator/gateway.pid"
if [[ -f "$pid_file" ]]; then
  old_pid="$(sed -E 's/.*"pid": ([0-9]+).*/\1/' "$pid_file")"
  kill -TERM "$old_pid" 2>/dev/null || true
  for _ in {1..30}; do
    new_pid="$(sed -E 's/.*"pid": ([0-9]+).*/\1/' "$pid_file" 2>/dev/null || true)"
    if [[ -n "$new_pid" && "$new_pid" != "$old_pid" ]] && kill -0 "$new_pid" 2>/dev/null; then
      echo "gateway restarted: $old_pid -> $new_pid"
      echo "update activated successfully"
      exit 0
    fi
    sleep 1
  done
  echo "gateway did not recover; restoring $backup_branch" >&2
  git reset --hard "$backup_branch"
  exit 1
fi

echo "update activated; gateway was not running"
