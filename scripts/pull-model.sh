#!/usr/bin/env bash
# Pre-warm the Hugging Face cache in the shared volume.
#
# Optional but recommended: without it the first `docker compose up` looks
# hung for 20-40 minutes while vLLM downloads tens of GB with no progress
# visible in `docker compose ps`. Here the download is in the foreground.
set -euo pipefail
source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"

require_env_file

MODEL="${1:-$(env_get MODEL_ID)}"
[[ -n "$MODEL" ]] || die "MODEL_ID is not set in .env"

VOLUME="$(env_get MODEL_CACHE_VOLUME llm-assistant-model-cache)"
IMAGE="$(env_get VLLM_IMAGE vllm/vllm-openai:v0.11.0)"
TOKEN="$(env_get HF_TOKEN)"

docker volume inspect "$VOLUME" >/dev/null 2>&1 \
  || die "volume '$VOLUME' does not exist. Run 'make infra' (terraform) first."

step "Downloading $MODEL into volume $VOLUME"
dim "reusing the vLLM image so the cache layout matches exactly"

docker run --rm -i \
  -e HF_HOME=/models \
  -e HF_TOKEN="$TOKEN" \
  -e HUGGING_FACE_HUB_TOKEN="$TOKEN" \
  -e HF_HUB_ENABLE_HF_TRANSFER=0 \
  -v "$VOLUME:/models" \
  --entrypoint python3 \
  "$IMAGE" - "$MODEL" <<'PY'
import sys
from huggingface_hub import snapshot_download

model = sys.argv[1]
print(f"resolving {model} ...", flush=True)
path = snapshot_download(
    repo_id=model,
    cache_dir="/models/hub",
    # Skip the duplicate formats vLLM will not read.
    ignore_patterns=["*.pth", "*.msgpack", "*.h5", "*.onnx", "original/*"],
    max_workers=8,
)
print(f"cached at {path}", flush=True)
PY

step "Cache size"
docker run --rm -v "$VOLUME:/models" --entrypoint du "$IMAGE" -sh /models | awk '{print "    " $1 "  " $2}'
ok "$MODEL is cached; 'make up' will now start in a couple of minutes"
