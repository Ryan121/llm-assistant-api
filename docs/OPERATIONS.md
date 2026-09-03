# Operations

## Layer map

Four tools, each owning exactly one thing:

| Layer | Owns | Entry point |
| --- | --- | --- |
| **Ansible** | The host: NVIDIA driver checks, Docker Engine, `nvidia-container-toolkit`, kernel limits | `make provision` |
| **Terraform** | Durable Docker resources: the bridge network and the model-cache volume | `make infra` |
| **Compose** | The containers: vLLM + gateway | `make up` |
| **Make** | Sequencing all of the above | `make quickstart` |

Why Terraform owns the volume rather than Compose: the model cache holds 60+ GB
that takes half an hour to refill. Declaring it `external:` in
`docker-compose.yml` means `docker compose down -v` — the command everyone
eventually runs — cannot touch it. Only an explicit `make infra-destroy` can,
and that prompts for the volume name first.

No Terraform on the host? `make infra-no-tf` creates the same two resources
with plain `docker` commands. Everything downstream is identical.

## Day-to-day

```bash
make ps                 # what is running
make health             # liveness, readiness, version
make logs               # everything
make logs-vllm          # just the model server (where the real errors are)
make gpu                # live utilisation, refreshed every 2s
make smoke              # end-to-end: auth, completion, streaming, tool calling
```

## Changing the model

```bash
vim .env                                    # edit MODEL_ID (and VLLM_TOOL_ARGS)
make pull-model                             # foreground download, with progress
make restart && make wait && make smoke
```

Both models stay in the cache volume, so switching back is fast.

## Upgrading the gateway

Images are published by CI to `ghcr.io/ryan121/llm-assistant-api` with semver
tags derived from Conventional Commits.

```bash
sed -i 's/^API_IMAGE_TAG=.*/API_IMAGE_TAG=1.4.2/' .env
make up                 # pulls and recreates the gateway only
make health             # /version confirms what is actually running
```

`API_IMAGE_TAG=local` (the default) uses whatever `make build` produced from
your working tree. Pin a real version for anything shared.

Rolling back is the same edit with an older tag — the model is untouched
because it was never in the image.

## Upgrading vLLM

```bash
sed -i 's|^VLLM_IMAGE=.*|VLLM_IMAGE=vllm/vllm-openai:v0.11.1|' .env
make check-images       # fails fast if that tag does not exist
make restart && make wait && make smoke
```

Pin the tag. A floating `:latest` will eventually change a CLI flag under you
and the stack will fail to start with a bare argparse error.

## Tuning

Everything below lives in `.env`; `make restart` applies it.

| Symptom | Knob | Direction |
| --- | --- | --- |
| CUDA OOM at startup | `GPU_MEMORY_UTILIZATION` | down to `0.85` |
| CUDA OOM at startup | `MAX_MODEL_LEN` | halve it |
| "Maximum concurrency" below 2 | `MAX_MODEL_LEN` | down — this is the main lever |
| Queueing under multi-user load | `VLLM_MAX_NUM_SEQS` | up, if KV cache allows |
| One agent monopolising the GPU | `MAX_TOKENS_CAP` | set to e.g. `8192` |
| Long agent runs cut off | `REQUEST_TIMEOUT_SECONDS` | up |
| Gateway CPU-bound on many streams | `WEB_CONCURRENCY` | up to `4` |

## Known failure modes

**NCCL hangs at startup with no error.** Tensor-parallel workers cannot reach
each other. Two causes, in order of likelihood:

1. Shared memory too small — already handled by `ipc: host` in the Compose
   file. If you removed it, put it back.
2. No NVLink bridge between the cards, and peer-to-peer probing stalls. Set
   `NCCL_P2P_DISABLE=1` in `.env` and restart.

**`EngineDeadError` / `RPC call to execute_model timed out` mid-generation.**
Different problem from the one above: the server loaded fine and had been
answering. Read the dump before assuming load:

```
num_computed_tokens=[13201]   total_num_scheduled_tokens=1
kv_cache_usage=0.0429
```

That is a *single-token decode step* with the KV cache 4% full — work that
takes tens of milliseconds when healthy. The timeout is
`VLLM_EXECUTE_MODEL_TIMEOUT_SECONDS`, default **300 s**, so a worker went five
minutes without answering. Note also which process died first: if the workers
log `Parent process exited, terminating worker`, they were alive and the engine
core gave up on them — the GPU is wedged inside a collective, not crashed.

Raising the timeout is not a fix. Diagnose:

```bash
nvidia-smi topo -m                        # NV1/NV2 = NVLink bridge; PHB/PXB/SYS = PCIe only
nvidia-smi nvlink -s                      # empty output means no bridge fitted
sudo dmesg -T | grep -iE 'xid|nvrm'       # needs root; see the Xid table below
nvidia-smi -q -d POWER,TEMPERATURE        # sustained throttling or a marginal PSU
```

- **`Xid` numbers are not interchangeable** — read which one it is before
  suspecting the hardware:

  | Xid | Meaning | Verdict |
  | --- | --- | --- |
  | 13, 31, 43 | Illegal memory access / illegal instruction / channel reset. NVIDIA's catalog calls Xid 43 a *"software induced fault"* and its recommended action is **IGNORE** — *"not indicative of a driver bug but rather a user application error"*. | **Not your cards.** The CUDA work stream did something invalid. |
  | 48, 62, 63, 64, 94, 95 | Double-bit ECC, uncorrectable memory errors | Hardware |
  | 79 | "GPU has fallen off the bus" | Hardware — power, riser, reseat |

  **The discriminator on a multi-GPU box is whether the Xid hit both ranks at
  the same instant.** A genuine application bug faults one worker. Both cards
  logging Xid 43 in the same second, with the pids of both tensor-parallel
  workers, means the illegal operation was *inside a collective* — a peer-to-peer
  access the IOMMU refused. That is the interconnect, not the model.

  ```
  NVRM: Xid (PCI:0000:05:00): 43, pid=29468, name=python3, Ch 00000008
  NVRM: Xid (PCI:0000:06:00): 43, pid=29469, name=python3, Ch 00000008
  ```

  Note also that the Xid lands *before* the `EngineDeadError`, typically by
  around the timeout value: the channels are reset first, after which no worker
  can complete another CUDA op, and the engine core spends its full
  `VLLM_EXECUTE_MODEL_TIMEOUT_SECONDS` waiting for a reply that can never come.
  Do not go looking for a second fault at the moment of the crash — there isn't
  one.
- **`topo -m` shows `PHB`/`NODE`/`SYS` with `nvlink -s` reporting `inActive`,
  and either no Xid or a simultaneous Xid 43 on both cards** → the
  interconnect. Peer-to-peer between the cards goes over
  PCIe via the host bridge, and vLLM's own all-reduce kernel — enabled by
  default, visible as `disable_custom_all_reduce=False` in the startup config
  dump — needs P2P to work. It does not check: `VLLM_SKIP_P2P_CHECK` defaults
  to `1`, so vLLM trusts the driver's report rather than verifying. Fix:

  ```bash
  NCCL_P2P_DISABLE=1
  VLLM_SKIP_P2P_CHECK=0
  VLLM_EXTRA_ARGS=--enable-prefix-caching --disable-log-requests --disable-custom-all-reduce
  ```

  All three are needed. `NCCL_P2P_DISABLE` only affects NCCL, and the custom
  all-reduce kernel uses CUDA IPC directly — so disabling P2P for NCCL alone
  leaves the broken path in use. There is no `VLLM_DISABLE_CUSTOM_ALL_REDUCE`
  env var; the CLI flag is the only unconditional switch.

  **Then prove it landed.** Editing `.env` is not the same as restarting the
  engine, and vLLM states its effective configuration only once, in a single
  line at startup:

  ```bash
  make show-config    # must report disable_custom_all_reduce=True
  ```

  That target compares three things — what `.env` asks for, what Compose
  passes, and what the running engine reports — and tells you which stage lost
  your change. It also warns about duplicate keys in `.env`, where the last
  definition silently wins.

**The same root cause has a second signature: corrupted data instead of a
hang.** Look for any of these, which arrive together:

```
TypeError: argument 'id': StreamInput must be either an integer or a list of integers
ScatterGatherKernel.cu:163: Assertion `idx_dim >= 0 && idx_dim < index_size' failed
torch.AcceleratorError: CUDA error: device-side assert triggered
RuntimeError: Worker failed with error 'CUDA error: device-side assert triggered'
```

Read that chain backwards. A GPU kernel indexed out of bounds, so the sampled
token ids are garbage; the detokenizer is then handed a token id that is
negative or beyond the vocabulary and its Rust binding rejects the type. For a
MoE model the out-of-bounds index is usually the expert router: corrupted
logits produce a top-k expert index outside the expert count, and the gather
asserts.

The detokenizer `TypeError` often appears *first* in the log because the API
server is a separate process and receives the bad ids before the worker reaches
its next CUDA sync point. It is a symptom, not the fault. CUDA errors are
asynchronous — the log says so itself — so do not trust the reported stack
location.

Both TP workers asserting in the same millisecond, with
`disable_custom_all_reduce=False` in the dump, means the corruption came from
the collective. Same fix as above. A hang and a device-side assert are the two
ways one broken interconnect presents itself; which one you get is luck.

**On a VM with passed-through GPUs this is close to a certainty, not a
possibility.** The ACS that passthrough needs for per-device IOMMU groups
forces device-to-device DMA up through the root complex, so real P2P either
does not work or is unstable — while CUDA still advertises it. `make preflight`
now detects both conditions and warns before you deploy.

If it still hangs after disabling P2P, stop fighting the interconnect:

1. **`TENSOR_PARALLEL_SIZE=1` with `PIPELINE_PARALLEL_SIZE=2`.** Pipeline
   parallelism splits by layer and passes activations point-to-point at one
   boundary, instead of all-reducing at every layer. Slightly higher latency,
   far less to go wrong.
2. **One card, INT4.** An AWQ/GPTQ build of a 30B model is ~17 GB, so it fits a
   single A6000 with a large KV cache and issues no collective at all. Set
   `TENSOR_PARALLEL_SIZE=1`, `CUDA_VISIBLE_DEVICES=0` and
   `--quantization awq_marlin`. On a genuinely bad interconnect this is the
   most robust configuration available, and Marlin INT4 is fast on Ampere.

Change one thing at a time and `make restart && make wait && make smoke` after
each, so you know which one mattered.

The stack recovers on its own: `restart: unless-stopped` brings vLLM back, the
gateway stays up throughout and reports the gap honestly — 502 on in-flight
requests, then `/readyz` going from `loading` back to `ready`. The lost request
is not retried.

**`could not select device driver "nvidia"`.** The container toolkit is not
registered with Docker. `make provision` fixes it; it also restarts Docker,
which is required for the change to take.

**`docker: permission denied` on the socket.** `make provision` added you to the
`docker` group, but this login session was created before that and still
carries the old supplementary groups. The playbook stops here on purpose,
because Terraform, Compose and `make up` all talk to the socket as you:

```bash
newgrp docker && make quickstart     # new group in this shell
sg docker -c 'make quickstart'       # one-off, no re-login
```

Or reconnect the SSH session. Provisioning is already done, so re-running it
after re-login is a no-op.

**Model download fails with 401/403.** The repo is gated. Accept its licence on
Hugging Face, put a token in `HF_TOKEN`, then `make pull-model`.

**Startup dies with `Bfloat16 is only supported on GPUs with compute
capability of at least 8.0`.** You are not on the hardware this is written for.

**`/readyz` returns 503 forever.** The gateway is fine; vLLM is not. The reason
is always in its log:

```bash
make logs-vllm | tail -80
```

**Disk fills up.** The cache accumulates every model you have tried.

```bash
docker run --rm -v llm-assistant-model-cache:/models alpine du -sh /models/hub/*
docker run --rm -v llm-assistant-model-cache:/models alpine \
  rm -rf /models/hub/models--org--model-you-no-longer-want
```

## Security posture

This is built for a trusted network. Defaults worth knowing:

- The gateway binds `0.0.0.0:8081` **inside the container**, published to the
  host port `API_PORT`. Anyone who can reach that port and holds a key can use
  the GPU. `make env` generates a key so it is never accidentally open, and the
  gateway logs a warning on every start if `API_KEYS` is blank.
- vLLM's own port is **not** published — it is reachable only on the internal
  Docker network. Set `UPSTREAM_API_KEY` as well if the host is shared.
- The gateway image runs as uid 10001, non-root, with no shell needed at
  runtime.
- Client tokens are never forwarded upstream; the gateway substitutes
  `UPSTREAM_API_KEY`. There is a unit test asserting this.
- CORS is off unless `CORS_ORIGINS` is set. VS Code does not need it; only
  browser-based UIs do.
- Exposing this beyond a LAN means putting TLS in front of it. The gateway
  honours `--proxy-headers`, so a reverse proxy terminating TLS works without
  changes.

## Backup

Two things matter, and neither is the model:

```bash
cp .env ~/secure-backup/llm-assistant.env          # keys and configuration
tar -C terraform -czf ~/secure-backup/tfstate.tgz terraform.tfstate
```

The weights are re-downloadable; the API keys and Terraform state are not.
