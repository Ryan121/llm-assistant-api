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

- The gateway binds `0.0.0.0:8080` **inside the container**, published to the
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
