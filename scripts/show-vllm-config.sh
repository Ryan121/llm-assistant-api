#!/usr/bin/env bash
# Three-way comparison of what you asked for, what Compose passes, and what the
# running engine actually reports.
#
# This exists because editing .env is not the same as changing the running
# server, and vLLM only tells you its effective configuration in one log line
# at startup. If those three disagree you can stop guessing.
set -euo pipefail
source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"

require_env_file

KEYS=(
  MODEL_ID
  TENSOR_PARALLEL_SIZE
  PIPELINE_PARALLEL_SIZE
  MAX_MODEL_LEN
  GPU_MEMORY_UTILIZATION
  VLLM_DTYPE
  VLLM_TOOL_ARGS
  VLLM_EXTRA_ARGS
  NCCL_P2P_DISABLE
  VLLM_SKIP_P2P_CHECK
)

step "1. What .env asks for"
for key in "${KEYS[@]}"; do
  value="$(env_get "$key")"
  printf '    %-24s %s\n' "$key" "${value:-<unset, compose default applies>}"
done

# A duplicate key silently wins over the earlier one and is very easy to create
# by appending to the file.
dupes="$(grep -oE '^[A-Z_]+=' "$ENV_FILE" | sort | uniq -d | tr -d '=' || true)"
if [[ -n "$dupes" ]]; then
  echo
  warn "these keys appear more than once in .env - the LAST one wins:"
  printf '      %s\n' $dupes >&2
fi

step "2. What Compose will actually pass to vLLM"
if ! compose config >/dev/null 2>&1; then
  die "docker compose config failed - fix the errors above first"
fi
# A state machine, not an awk range: the range end pattern also matches the
# line the range starts on, which silently yields nothing.
rendered="$(compose config 2>/dev/null | awk '
  $0 == "  vllm:"                { in_service = 1; next }
  in_service && /^  [^ ]/        { in_service = 0 }
  in_service && /^    command:/  { in_command = 1; next }
  in_command && /^      - /      { sub(/^      - /, ""); printf "%s ", $0; next }
  in_command && /^    [^ ]/      { in_command = 0 }
' || true)"

if [[ -z "$rendered" ]]; then
  warn "could not extract the vllm command from 'docker compose config'"
else
  printf '%s\n' "$rendered" | fold -s -w 76 | sed 's/^/    /'
fi

step "3. What the running engine reports"
dump="$(compose logs --no-log-prefix vllm 2>/dev/null \
  | grep -F 'LLM engine' | tail -1 || true)"

if [[ -z "$dump" ]]; then
  warn "no startup config line found in the vllm log."
  warn "The container may not be running, or the log has rotated past startup."
  info "Try: make ps   /   make logs-vllm"
  exit 0
fi

for field in tensor_parallel_size pipeline_parallel_size disable_custom_all_reduce \
             enforce_eager max_seq_len quantization kv_cache_dtype \
             enable_prefix_caching dtype; do
  value="$(printf '%s' "$dump" | grep -oE "${field}=[^,]*" | head -1 | cut -d= -f2- || true)"
  printf '    %-28s %s\n' "$field" "${value:-?}"
done

kv="$(compose logs --no-log-prefix vllm 2>/dev/null \
  | grep -iE 'KV cache size|Maximum concurrency' | tail -2 || true)"
if [[ -n "$kv" ]]; then
  echo
  printf '%s\n' "$kv" | sed 's/^/    /'
fi

# The whole point of the script: catch intent that never reached the engine.
step "Verdict"
running_tp="$(printf '%s' "$dump" | grep -oE 'tensor_parallel_size=[0-9]+' | cut -d= -f2 || echo '?')"
wanted_tp="$(env_get TENSOR_PARALLEL_SIZE 2)"
running_car="$(printf '%s' "$dump" | grep -oE 'disable_custom_all_reduce=[A-Za-z]+' | cut -d= -f2 || echo '?')"
wants_car_off=false
[[ "$(env_get VLLM_EXTRA_ARGS)" == *--disable-custom-all-reduce* ]] && wants_car_off=true

mismatch=false
if [[ "$running_tp" != "$wanted_tp" ]]; then
  warn "TENSOR_PARALLEL_SIZE is $wanted_tp in .env but the engine is running $running_tp"
  mismatch=true
fi
if $wants_car_off && [[ "$running_car" != "True" ]]; then
  warn ".env passes --disable-custom-all-reduce but the engine reports"
  warn "  disable_custom_all_reduce=$running_car - the flag did NOT take effect"
  mismatch=true
fi
if ! $wants_car_off && [[ "$running_car" == "False" ]]; then
  info "custom all-reduce is ENABLED (the default)."
  dim "On a PHB/no-NVLink or virtualised host this is the usual cause of"
  dim "device-side asserts and mid-run hangs. See MULTI-GPU STABILITY in"
  dim ".env.example."
fi

if $mismatch; then
  echo
  die "the running engine does not match .env - run 'make restart' (not 'make up')"
fi
ok "the running engine matches .env"
