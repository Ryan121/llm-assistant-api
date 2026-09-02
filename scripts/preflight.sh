#!/usr/bin/env bash
# Fail fast, before anything downloads 60 GB, on the things that actually
# break this deployment.
set -euo pipefail
source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"

failures=0
fail() { warn "$*"; failures=$((failures + 1)); }

step "Toolchain"
if have docker; then
  ok "docker $(docker --version | awk '{print $3}' | tr -d ,)"
else
  fail "docker is not installed"
fi

if docker compose version >/dev/null 2>&1; then
  ok "docker compose $(docker compose version --short 2>/dev/null || echo v2)"
else
  fail "the 'docker compose' v2 plugin is missing (docker-compose v1 is not supported)"
fi

docker info >/dev/null 2>&1 && ok "docker daemon reachable" \
  || fail "cannot talk to the docker daemon (is it running? are you in the docker group?)"

for tool in terraform ansible-playbook make; do
  have "$tool" && ok "$tool present" || warn "$tool not found - 'make deps' prints install commands"
done

step "GPUs"
if have nvidia-smi; then
  mapfile -t gpus < <(nvidia-smi --query-gpu=name,memory.total,driver_version --format=csv,noheader)
  if (( ${#gpus[@]} == 0 )); then
    fail "nvidia-smi reported no GPUs"
  else
    for gpu in "${gpus[@]}"; do ok "$gpu"; done
    tp="$(env_get TENSOR_PARALLEL_SIZE 2)"
    (( ${#gpus[@]} >= tp )) \
      && ok "${#gpus[@]} GPU(s) available for TENSOR_PARALLEL_SIZE=$tp" \
      || fail "TENSOR_PARALLEL_SIZE=$tp but only ${#gpus[@]} GPU(s) present"
  fi

  driver="$(nvidia-smi --query-gpu=driver_version --format=csv,noheader | head -1)"
  major="${driver%%.*}"
  (( major >= 535 )) && ok "driver $driver" \
    || fail "driver $driver is older than 535; vLLM's CUDA 12.x wheels need >= 535"

  if docker run --rm --gpus all --entrypoint nvidia-smi \
       "${NVIDIA_SMOKE_IMAGE:-nvidia/cuda:12.4.1-base-ubuntu22.04}" -L >/dev/null 2>&1; then
    ok "containers can see the GPUs"
  else
    fail "containers cannot see the GPUs - install nvidia-container-toolkit ('make provision')"
  fi
else
  fail "nvidia-smi not found - install the NVIDIA driver on this host"
fi

step "Disk"
docker_root="$(docker info --format '{{.DockerRootDir}}' 2>/dev/null || echo /var/lib/docker)"
avail_gb="$(df -BG "$docker_root" 2>/dev/null | awk 'NR==2 {gsub(/G/,"",$4); print $4}' || echo 0)"
required_gb="${REQUIRED_FREE_DISK_GB:-200}"
if (( avail_gb >= required_gb )); then
  ok "${avail_gb} GB free on ${docker_root}"
else
  fail "${avail_gb} GB free on ${docker_root}, need >= ${required_gb} GB for the model cache"
fi

step "Configuration"
if [[ -f "$ENV_FILE" ]]; then
  ok ".env present"
  model="$(env_get MODEL_ID)"
  [[ -n "$model" ]] && ok "MODEL_ID=$model" || fail "MODEL_ID is empty in .env"
  [[ -n "$(env_get API_KEYS)" ]] \
    && ok "API_KEYS is set" \
    || warn "API_KEYS is empty - the gateway will accept unauthenticated requests"
else
  fail ".env is missing - run 'make env'"
fi

echo
if (( failures > 0 )); then
  die "$failures blocking problem(s) found above."
fi
printf '%sPreflight passed.%s\n' "$C_GREEN" "$C_RESET"
