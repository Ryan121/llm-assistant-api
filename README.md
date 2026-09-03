# llm-assistant-api

A self-hosted coding assistant for **2× RTX A6000**, deployed in one command
and driven from VS Code.

[![CI](https://github.com/Ryan121/llm-assistant-api/actions/workflows/ci.yml/badge.svg)](https://github.com/Ryan121/llm-assistant-api/actions/workflows/ci.yml)
[![Release](https://github.com/Ryan121/llm-assistant-api/actions/workflows/release.yml/badge.svg)](https://github.com/Ryan121/llm-assistant-api/actions/workflows/release.yml)

```bash
make quickstart
```

That prepares the host, provisions the Docker resources, downloads the model,
starts the stack, waits for it to serve, proves it works end to end, and prints
your VS Code configuration.

Restart & test

```bash
make restart && make wait && make smoke
```

---

## What you get

```
  VS Code                    host                                GPUs
┌───────────┐        ┌──────────────────────┐
│ Continue  │        │  llm-assistant-api   │  ┌───────────┐   ┌──────────┐
│ Cline     │──────▶ │  FastAPI gateway     │─▶│   vLLM    │──▶│ A6000 #0 │
│ Roo Code  │  :8081 │  · bearer auth       │  │  TP = 2   │   ├──────────┤
│ Copilot   │  /v1   │  · model routing     │  │  BF16     │──▶│ A6000 #1 │
└───────────┘        │  · SSE streaming     │  └───────────┘   └──────────┘
                     │  · token ceiling     │        │
                     └──────────────────────┘        ▼
                          semver image        model-cache volume
                          (never has the        (Hugging Face,
                           model inside)         pulled at runtime)
```

- **OpenAI-compatible API** — `/v1/models`, `/v1/chat/completions`,
  `/v1/completions`, streaming and blocking. Any editor extension that takes a
  custom base URL just works.
- **The model is an environment variable.** `MODEL_ID` in `.env`. It is fetched
  from Hugging Face at runtime into a named volume, never baked into an image,
  so the gateway image stays ~60 MB and switching models is a two-line change.
- **Semantically versioned gateway image**, published to GHCR from Conventional
  Commits. No hand-edited version numbers.
- **Terraform + Ansible + Compose**, each owning one layer, all sequenced by the
  Makefile.
- **Tested.** 61 hermetic unit tests, 100% coverage, `mypy --strict`, plus
  Terraform validate, Ansible lint and shellcheck in CI.

## The model

**`Qwen/Qwen3-Coder-30B-A3B-Instruct`** — MoE, ~30 B total / ~3 B active.

It fits in BF16 across the pair (~61 GB of weights → ~31 GB per card at TP=2)
with room left for a 128 K KV cache, decodes at roughly small-dense-model speed
because so few parameters are active, and has native tool calling — which is
what makes agent mode in VS Code work at all. The A6000 is Ampere and has no
FP8 tensor cores, so BF16 is the right dtype and a 3 B-active MoE is how you
get quality without paying 32 B-dense latency.

Alternatives (Devstral-Small, Qwen2.5-Coder-32B, GLM-4.5-Air), the full VRAM
arithmetic, and the tool-parser matrix: **[docs/MODEL_SELECTION.md](docs/MODEL_SELECTION.md)**.

## Requirements

| | |
| --- | --- |
| GPUs | 2× RTX A6000 (48 GB each). Any 2 GPUs ≥ 40 GB work. |
| Driver | NVIDIA ≥ 535 (for vLLM's CUDA 12.x wheels) |
| OS | Linux. Docker install/toolkit automation targets Debian/Ubuntu. |
| Disk | ≥ 200 GB free wherever Docker stores volumes |
| Tools | `docker` + Compose v2, `make`, `terraform`, `ansible-core`, `curl` |

`make deps` tells you what is missing and how to install it. `make preflight`
verifies the GPUs, driver, container GPU passthrough and disk before anything
large is downloaded.

Ansible deliberately does **not** install GPU drivers — a bad driver swap can
leave a box needing physical access to recover. It checks and tells you.

## Installation

### One command

```bash
make quickstart
```

Which runs, in order:

| Step | What it does |
| --- | --- |
| `env` | Copies `.env.example` → `.env` and mints an API key |
| `deps` | Checks the toolchain |
| `provision` | Ansible: driver/GPU asserts, Docker, `nvidia-container-toolkit`, kernel limits |
| `infra` | Terraform: bridge network + model-cache volume |
| `preflight` | Verifies `docker run --gpus all` actually works |
| `build` | Builds the gateway image |
| `pull-model` | Downloads `MODEL_ID` in the foreground, with progress |
| `up` | `docker compose up -d` |
| `wait` | Blocks on `/readyz` until the model is serving |
| `smoke` | Auth, a real completion, streaming, and a tool call |
| `vscode-config` | Prints your editor configuration |

Expect 30–60 minutes on first run; almost all of it is the ~61 GB download.
Subsequent `make up` is under two minutes.

### Or step by step

```bash
make env                # then edit .env: MODEL_ID, API_KEYS
make provision          # add ANSIBLE_ARGS=-K if sudo needs a password
make infra              # or: make infra-no-tf, if you have no Terraform
make preflight
make build pull-model up wait smoke
```

### Deploying to a remote GPU box

Point the inventory and Docker host at it; nothing else changes.

```bash
# ansible/inventory/hosts.ini
[gpu_hosts]
gpu-01 ansible_host=10.0.0.21 ansible_user=ubuntu
```

```bash
make ansible-deps
DOCKER_HOST=ssh://ubuntu@10.0.0.21 make env deps provision infra
make deploy-remote                       # rsyncs the repo, builds and starts it there
GATEWAY_HOST=10.0.0.21 make vscode-config
```

## Connect VS Code

Use **[Continue](https://marketplace.visualstudio.com/items?itemName=Continue.continue)**
— no third-party account, and one config covers chat, inline edit, agent mode
and autocomplete:

```bash
code --install-extension Continue.continue
make vscode-config      # print config for every supported extension
make vscode-install     # write ~/.continue/config.yaml (backs up any existing one)
```

| | |
| --- | --- |
| Base URL | `http://127.0.0.1:8081/v1` |
| API key | first entry of `API_KEYS` in `.env` |
| Model | value of `MODEL_ID` |

If VS Code runs somewhere other than the GPU box, tunnel rather than exposing
the port — there is no TLS in front of the gateway:

```bash
ssh -N -L 8081:127.0.0.1:8081 ubuntu@gpu-01
```

Cline/Roo Code also work (OpenAI Compatible provider). The **built-in VS Code
chat** can be pointed here too, but the model picker and its bring-your-own-key
flow belong to the GitHub Copilot Chat extension rather than to VS Code, so it
needs that extension plus a signed-in Copilot entitlement — and it replaces the
chat model only, leaving completions with GitHub.

Per-extension walkthroughs and troubleshooting: **[docs/VSCODE.md](docs/VSCODE.md)**.

## Configuration

Everything is one environment variable in `.env` — the single source of truth
for Compose, the gateway, Terraform (via `scripts/env-to-tfvars.sh`) and
Ansible. The ones that matter most:

| Variable | Default | |
| --- | --- | --- |
| `MODEL_ID` | `Qwen/Qwen3-Coder-30B-A3B-Instruct` | Any Hugging Face repo vLLM can serve |
| `API_KEYS` | generated | Comma-separated bearer tokens. **Empty means no auth.** |
| `API_PORT` | `8081` | Host port for the gateway |
| `TENSOR_PARALLEL_SIZE` | `2` | GPUs to shard across |
| `MAX_MODEL_LEN` | `131072` | Context window. The main lever on concurrency. |
| `GPU_MEMORY_UTILIZATION` | `0.90` | Drop to `0.85` if you hit OOM at startup |
| `VLLM_TOOL_ARGS` | `--tool-call-parser qwen3_coder` | **Must match the model family** |
| `API_IMAGE_TAG` | `local` | Set to a published semver to pin the gateway |
| `MAX_TOKENS_CAP` | `0` | Set to bound a runaway agent loop |
| `AUTOCOMPLETE_ENABLED` | `false` | Add a small FIM model for inline completion |

The annotated full list is in [`.env.example`](.env.example); tuning guidance
is in [docs/OPERATIONS.md](docs/OPERATIONS.md#tuning).

## How the layers divide

| Layer | Owns | Command |
| --- | --- | --- |
| Ansible | The host — driver checks, Docker, container toolkit, kernel limits | `make provision` |
| Terraform | Durable Docker resources — network, model-cache volume | `make infra` |
| Compose | The containers — vLLM and the gateway | `make up` |
| Make | Sequencing | `make quickstart` |

Terraform owns the model-cache volume for one concrete reason: declared
`external:` in Compose, it cannot be destroyed by `docker compose down -v`.
Only an explicit `make infra-destroy` can, and that prompts for the volume name
first. 60 GB and half an hour is too much to lose to a reflex.

## Development

```bash
make venv         # .venv with dev extras
make hooks        # commit-msg hook enforcing Conventional Commits
make validate     # ruff, mypy --strict, pytest, terraform validate
make run-local    # gateway on the host with --reload, against a running vLLM
```

Tests are hermetic — no GPU, no network, no Docker. The vLLM upstream is faked
with `httpx.MockTransport`, so the real routing, auth, header and SSE-relay
paths are exercised. Coverage is gated at 90% in CI.

```
$ make test
............................................................. [100%]
TOTAL     313      0     46      0   100%
61 passed
```

## Releases

The version is never set by hand. `python-semantic-release` reads the commits
merged to `main`:

| Prefix | Bump |
| --- | --- |
| `fix:` `perf:` `refactor:` | patch |
| `feat:` | minor |
| `feat!:` / `BREAKING CHANGE:` | major |

CI then tags `vX.Y.Z`, writes `CHANGELOG.md`, and publishes
`ghcr.io/ryan121/llm-assistant-api` at `X.Y.Z`, `X.Y`, `X` and `latest`.
Details in [CONTRIBUTING.md](CONTRIBUTING.md).

## Operations

Health, logs, tuning, upgrades, known failure modes (NCCL hangs, gated repos,
`could not select device driver "nvidia"`), security posture and backup:
**[docs/OPERATIONS.md](docs/OPERATIONS.md)**.

```bash
make health   make logs-vllm   make gpu   make smoke   make down
```

## Layout

```
├── Makefile                  every workflow, self-documenting (make help)
├── docker-compose.yml        vLLM + gateway; model volume is external
├── Dockerfile                gateway only; the model is never baked in
├── .env.example              annotated single source of truth
├── src/llm_assistant_api/    the FastAPI gateway
│   ├── config.py             every knob, as env vars
│   ├── proxy.py              upstream routing, payload rules, SSE relay
│   ├── deps.py               bearer auth
│   └── routes/               /healthz /readyz /version and the /v1 surface
├── tests/                    61 hermetic tests, mocked upstream
├── terraform/                network + model-cache volume
├── ansible/                  host prep (site.yml), remote deploy (deploy.yml)
├── scripts/                  preflight, model pull, readiness, smoke, editor config
├── .github/workflows/        ci.yml (test/lint/build), release.yml (semver + GHCR)
└── docs/                     MODEL_SELECTION · VSCODE · OPERATIONS
```

## Licence

MIT — see [LICENSE](LICENSE).
