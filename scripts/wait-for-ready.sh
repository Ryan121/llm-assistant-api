#!/usr/bin/env bash
# Poll /readyz until the model is actually serving.
#
# A cold start is dominated by the Hugging Face download, so the default
# timeout is generous. On failure it dumps the vLLM log tail, because that is
# where the real reason (OOM, bad --max-model-len, gated repo) always is.
set -euo pipefail
source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"

require_env_file

TIMEOUT="${READY_TIMEOUT:-3600}"
INTERVAL="${READY_INTERVAL:-10}"
BASE="$(api_base_url)"

step "Waiting for $BASE/readyz (timeout ${TIMEOUT}s)"

deadline=$(( $(date +%s) + TIMEOUT ))
last_state=""
spinner=('|' '/' '-' '\')
spin=0

while (( $(date +%s) < deadline )); do
  body="$(curl -fsS --max-time 10 "$BASE/readyz" 2>/dev/null || true)"

  if [[ -n "$body" ]] && printf '%s' "$body" | grep -q '"status":"ready"'; then
    echo
    ok "model is serving"
    printf '%s\n' "$body" | sed 's/^/    /'
    exit 0
  fi

  if [[ -z "$body" ]]; then
    state="gateway not answering yet"
  else
    state="model loading"
  fi

  if [[ "$state" != "$last_state" ]]; then
    echo
    info "$state"
    last_state="$state"
  fi

  # Surface the vLLM progress line so a long wait does not look like a hang.
  progress="$(compose logs --no-log-prefix --tail 40 vllm 2>/dev/null \
    | grep -Eo '(Loading safetensors[^ ]*|[0-9]+%\|[^|]*\| *[0-9]+/[0-9]+)' | tail -1 || true)"
  printf '\r    %s %s%s%s   ' "${spinner[$spin]}" "$C_DIM" "${progress:-still working}" "$C_RESET"
  spin=$(( (spin + 1) % 4 ))

  sleep "$INTERVAL"
done

echo
warn "gave up after ${TIMEOUT}s"
step "Last 60 lines from vllm"
compose logs --tail 60 vllm 2>&1 | sed 's/^/    /' || true
step "Container status"
compose ps 2>&1 | sed 's/^/    /' || true
die "the model never became ready - see the log above"
