#!/usr/bin/env bash
# Load-test the deployment and report the numbers that tuning decisions need:
# time to first token, inter-token latency, and throughput under concurrency.
#
# Every vLLM flag worth arguing about -- speculative decoding, fp8 KV cache,
# max-model-len against max-num-seqs -- is a trade. Without a repeatable
# measurement you are not tuning, you are guessing, and `make smoke` only ever
# proves the stack answers at all.
#
# The default profile is shaped like agent traffic rather than chat: a long
# prompt (the repo context an agent carries) and a short completion (an edit,
# not an essay). Override any of it from the environment.
set -euo pipefail
source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"

require_env_file
have docker || die "docker is required"

BASE="$(api_base_url)"
MODEL="$(env_get MODEL_ID)"
API_KEY="$(env_get API_KEYS)"; API_KEY="${API_KEY%%,*}"
VLLM_IMAGE="$(env_get VLLM_IMAGE vllm/vllm-openai:v0.11.0)"
NETWORK="$(env_get DOCKER_NETWORK llm-assistant-net)"
CACHE_VOLUME="$(env_get MODEL_CACHE_VOLUME llm-assistant-model-cache)"

# Agent-shaped by default: big prefix, small output, a handful of callers.
PROMPTS="${BENCH_PROMPTS:-40}"
CONCURRENCY="${BENCH_CONCURRENCY:-4}"
INPUT_LEN="${BENCH_INPUT_LEN:-8192}"
OUTPUT_LEN="${BENCH_OUTPUT_LEN:-256}"
# Varying the prefix stops prefix caching from turning the benchmark into a
# cache-hit test. Set to 0 deliberately to measure the cached path instead.
PREFIX_LEN="${BENCH_PREFIX_LEN:-1024}"

step "Benchmark profile"
info "model        $MODEL"
info "endpoint     $BASE/v1"
info "prompts      $PROMPTS at concurrency $CONCURRENCY"
info "shape        ${INPUT_LEN} in / ${OUTPUT_LEN} out (shared prefix ${PREFIX_LEN})"
dim  "override with BENCH_PROMPTS, BENCH_CONCURRENCY, BENCH_INPUT_LEN,"
dim  "BENCH_OUTPUT_LEN, BENCH_PREFIX_LEN"

# Readiness first: benchmarking a model that is still loading measures the
# download, and the resulting numbers are pure noise.
step "Readiness"
if ! curl -fsS --max-time 10 "$BASE/readyz" >/dev/null 2>&1; then
  die "gateway is not ready. Run 'make wait' first."
fi
ok "model is serving"

# The harness runs inside the vLLM image because that is where `vllm bench`
# and a matching tokenizer already live -- no extra host dependency, and the
# token counts line up with what the engine actually sees.
step "Running vllm bench serve"

# Built as an array so the bearer token survives as one argument. Inlined as
# ${VAR:+...} it would word-split into four.
bench_args=(
  bench serve
  --backend openai-chat
  --base-url "http://api:8081"
  --endpoint /v1/chat/completions
  --model "$MODEL"
  --tokenizer "$MODEL"
  --dataset-name random
  --num-prompts "$PROMPTS"
  --max-concurrency "$CONCURRENCY"
  --random-input-len "$INPUT_LEN"
  --random-output-len "$OUTPUT_LEN"
  --random-prefix-len "$PREFIX_LEN"
  --percentile-metrics ttft,tpot,itl,e2el
  --metric-percentiles 50,95,99
)
if [[ -n "$API_KEY" ]]; then
  bench_args+=(--header "Authorization: Bearer $API_KEY")
fi

docker run --rm \
  --network "$NETWORK" \
  -v "$CACHE_VOLUME:/models" \
  -e HF_HOME=/models \
  -e "HF_TOKEN=$(env_get HF_TOKEN)" \
  --entrypoint vllm \
  "$VLLM_IMAGE" \
  "${bench_args[@]}" \
  || die "benchmark failed. If the gateway rejected auth, check API_KEYS."

step "Next"
dim "Compare runs after changing one flag at a time in VLLM_EXTRA_ARGS."
dim "Highest-leverage first for agent traffic:"
dim "  --speculative-config '{\"method\":\"ngram\",\"num_speculative_tokens\":5}'"
dim "  --kv-cache-dtype fp8       (frees KV cache; verify output quality)"
dim "  MAX_MODEL_LEN=65536        (trades context for concurrent slots)"
dim "Read KV-cache utilisation and preemptions from: curl $BASE/metrics/upstream"
