#!/usr/bin/env bash
# Fail fast, before anything downloads 60 GB, on the things that actually
# break this deployment.
#
# Deliberately NOT `set -e`. This is a diagnostic: its job is to run every
# check and report all of them, so one probe exiting non-zero for a reason we
# did not anticipate must not abort the run. Every check records its own
# verdict through fail(), and the script exits non-zero at the end if any of
# them were blocking. `set -e` here previously turned an unexpected probe
# status into a bare "make: *** Error 141" with no output at all -- the exact
# opposite of what a preflight script is for.
set -uo pipefail
source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"

failures=0
fail() { warn "$*"; failures=$((failures + 1)); }

# Report the line and command behind an unexpected non-zero, so a future
# surprise is self-diagnosing instead of a naked exit code.
trap 'last_status=$?; last_command=$BASH_COMMAND; last_line=$LINENO' ERR
report_last_error() {
  if [[ -n "${last_status:-}" && "${last_status:-0}" -gt 128 ]]; then
    warn "a probe was killed by signal $(( last_status - 128 )) at line ${last_line:-?}:"
    warn "  ${last_command:-?}"
  fi
}

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
    pp="$(env_get PIPELINE_PARALLEL_SIZE 1)"
    needed=$(( tp * pp ))
    (( ${#gpus[@]} >= needed )) \
      && ok "${#gpus[@]} GPU(s) available for TP=$tp x PP=$pp" \
      || fail "TP=$tp x PP=$pp needs $needed GPU(s) but only ${#gpus[@]} present"
  fi

  # Tensor parallelism all-reduces once per layer, so it is the mode that cares
  # about the link between the cards. Catch a bad topology here rather than as
  # an EngineDeadError half an hour into real use.
  if (( $(env_get TENSOR_PARALLEL_SIZE 2) > 1 )); then
    # No `exit` in the awk: quitting early closes the pipe while nvidia-smi is
    # still printing its legend, which kills it with SIGPIPE and -- under
    # `set -o pipefail` -- aborts this whole script with status 141.
    # No `exit` in the awk: quitting on the first match closes the pipe while
    # nvidia-smi may still be writing its legend, which kills it with SIGPIPE.
    topo="$(nvidia-smi topo -m 2>/dev/null | awk '/^GPU0/ && !seen { print $3; seen = 1 }' || true)"
    case "$topo" in
      NV*)
        ok "GPU interconnect: $topo (NVLink) - tensor parallelism is a good fit"
        ;;
      PIX|PXB)
        ok "GPU interconnect: $topo (PCIe, no host bridge)"
        ;;
      PHB|NODE|SYS)
        warn "GPU interconnect: $topo - PCIe via the host bridge, no NVLink."
        warn "  Tensor parallelism can hang mid-generation on this topology."
        warn "  Recommended: NCCL_P2P_DISABLE=1 and add --disable-custom-all-reduce"
        warn "  to VLLM_EXTRA_ARGS. See MULTI-GPU STABILITY in .env.example."
        ;;
      *)
        dim "GPU interconnect: could not determine from nvidia-smi topo -m"
        ;;
    esac

    # Passed-through GPUs almost always mean ACS is on, which breaks P2P.
    if [[ -r /sys/class/dmi/id/sys_vendor ]] \
       && grep -qiE 'qemu|kvm|vmware|xen|virtual|microsoft corporation' \
            /sys/class/dmi/id/sys_vendor /sys/class/dmi/id/product_name 2>/dev/null; then
      warn "Virtualised host detected - GPUs are passed through."
      warn "  GPU peer-to-peer is unreliable under the ACS that passthrough needs."
      warn "  Set NCCL_P2P_DISABLE=1 and --disable-custom-all-reduce, or use"
      warn "  TENSOR_PARALLEL_SIZE=1 with PIPELINE_PARALLEL_SIZE=2."
    fi
  fi

  # awk 'NR==1' rather than `head -1`, for the same SIGPIPE reason as above.
  driver="$(nvidia-smi --query-gpu=driver_version --format=csv,noheader | awk 'NR == 1' || true)"
  major="${driver%%.*}"
  if [[ -z "$driver" ]]; then
    warn "could not read the driver version from nvidia-smi"
  elif (( major >= 535 )); then
    ok "driver $driver"
  else
    fail "driver $driver is older than 535; vLLM's CUDA 12.x wheels need >= 535"
  fi

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
avail_gb="$(df -BG "$docker_root" 2>/dev/null | awk 'NR==2 {gsub(/G/,"",$4); print $4}' || true)"
required_gb="${REQUIRED_FREE_DISK_GB:-200}"
if [[ -z "$avail_gb" ]]; then
  warn "could not read free space on ${docker_root} (df -BG unsupported here?)"
elif (( avail_gb >= required_gb )); then
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
report_last_error
if (( failures > 0 )); then
  die "$failures blocking problem(s) found above."
fi
printf '%sPreflight passed.%s\n' "$C_GREEN" "$C_RESET"
