# =============================================================================
# llm-assistant-api -- one command to stand up a local coding assistant.
#
#   make quickstart     everything: preflight -> terraform -> ansible ->
#                       build -> pull model -> up -> verify -> VS Code config
#   make help           every target
# =============================================================================

SHELL := /usr/bin/env bash
.SHELLFLAGS := -eu -o pipefail -c
.DEFAULT_GOAL := help

# .env is the single source of truth. It may not exist yet, hence `-include`.
ENV_FILE ?= .env
-include $(ENV_FILE)

API_PORT            ?= 8080
API_IMAGE           ?= ghcr.io/ryan121/llm-assistant-api
API_IMAGE_TAG       ?= local
MODEL_ID            ?= Qwen/Qwen3-Coder-30B-A3B-Instruct
DOCKER_NETWORK      ?= llm-assistant-net
MODEL_CACHE_VOLUME  ?= llm-assistant-model-cache
VLLM_IMAGE          ?= vllm/vllm-openai:v0.11.0

# Extra compose flags, e.g. make up PROFILES="--profile autocomplete"
PROFILES ?=

# Extra ansible flags, e.g. make provision ANSIBLE_ARGS=-K  (prompt for sudo)
ANSIBLE_ARGS ?=

PY        := .venv/bin/python
COMPOSE   := docker compose
TF        := terraform -chdir=terraform
PLAYBOOK  := ansible-playbook
GIT_SHA   := $(shell git rev-parse --short HEAD 2>/dev/null || echo unknown)
BUILD_DATE := $(shell date -u +%Y-%m-%dT%H:%M:%SZ)

export GIT_SHA BUILD_DATE

# -----------------------------------------------------------------------------
.PHONY: help
help: ## Show this help
	@printf '\033[1mllm-assistant-api\033[0m -- self-hosted coding assistant on 2x RTX A6000\n\n'
	@printf '  \033[32mmake quickstart\033[0m   full deployment from a clean checkout\n\n'
	@awk 'BEGIN {FS = ":.*## "} \
	  /^## ---/ { s = substr($$0, 8); gsub(/ *-+$$/, "", s); printf "\n\033[1m%s\033[0m\n", s; next } \
	  /^[a-zA-Z0-9_-]+:.*## / { printf "  \033[36m%-20s\033[0m %s\n", $$1, $$2 }' $(MAKEFILE_LIST)
	@printf '\n'

## --- One-shot deployment ---------------------------------------------------

# Order matters: provision installs Docker and the NVIDIA container toolkit,
# so it has to run before Terraform (which talks to the Docker socket) and
# before preflight (which verifies `docker run --gpus all` actually works).
.PHONY: quickstart
quickstart: env deps provision infra preflight build pull-model up wait smoke vscode-config ## Deploy everything, end to end
	@printf '\n\033[32m==> Ready.\033[0m Point VS Code at http://127.0.0.1:$(API_PORT)/v1\n'

.PHONY: up-fast
up-fast: env infra build up wait ## Redeploy without preflight/provision/model pull

## --- Configuration ---------------------------------------------------------

.PHONY: env
env: ## Create .env from .env.example and mint an API key
	@scripts/init-env.sh

.PHONY: deps
deps: ## Check that terraform/ansible/docker are installed
	@missing=0; \
	for t in docker terraform ansible-playbook curl; do \
	  if command -v $$t >/dev/null 2>&1; then printf '    \033[32m✓\033[0m %s\n' "$$t"; \
	  else printf '    \033[31m✗\033[0m %s\n' "$$t"; missing=1; fi; \
	done; \
	if [ $$missing -eq 1 ]; then \
	  printf '\nInstall the missing tools:\n'; \
	  printf '  Ubuntu/Debian  sudo apt-get install -y make curl python3-pip \\\n'; \
	  printf '                 && pipx install ansible-core \\\n'; \
	  printf '                 && sudo snap install terraform --classic\n'; \
	  printf '  macOS          brew install terraform ansible\n'; \
	  printf '  Docker         https://docs.docker.com/engine/install/\n'; \
	  exit 1; \
	fi

.PHONY: ansible-deps
ansible-deps: ## Install the optional Ansible collections (needed for remote hosts)
	@ansible-galaxy collection install -r ansible/requirements.yml

.PHONY: preflight
preflight: ## Verify GPUs, driver, disk and container GPU access
	@scripts/preflight.sh

.PHONY: hooks
hooks: ## Install the commit-msg hook that enforces Conventional Commits
	@git config core.hooksPath .githooks && chmod +x .githooks/* \
	  && printf '    \033[32m✓\033[0m core.hooksPath -> .githooks\n'

## --- Infrastructure --------------------------------------------------------

.PHONY: infra
infra: tfvars ## terraform apply: docker network + model-cache volume
	@$(TF) init -input=false -upgrade >/dev/null
	@$(TF) apply -input=false -auto-approve
	@printf '    \033[32m✓\033[0m network=%s volume=%s\n' "$(DOCKER_NETWORK)" "$(MODEL_CACHE_VOLUME)"

.PHONY: tfvars
tfvars: ## Render terraform/terraform.tfvars from .env
	@scripts/env-to-tfvars.sh

.PHONY: plan
plan: tfvars ## terraform plan
	@$(TF) init -input=false >/dev/null && $(TF) plan

.PHONY: infra-destroy
infra-destroy: ## terraform destroy -- DELETES the model cache volume
	@printf 'This removes the %s volume and its ~60 GB of weights.\n' "$(MODEL_CACHE_VOLUME)"
	@read -r -p 'Type the volume name to confirm: ' answer; \
	  [ "$$answer" = "$(MODEL_CACHE_VOLUME)" ] || { echo 'aborted'; exit 1; }
	@$(TF) destroy -input=false -auto-approve

.PHONY: infra-no-tf
infra-no-tf: ## Escape hatch: create the network/volume with plain docker
	@docker network inspect $(DOCKER_NETWORK) >/dev/null 2>&1 \
	  || docker network create $(DOCKER_NETWORK)
	@docker volume inspect $(MODEL_CACHE_VOLUME) >/dev/null 2>&1 \
	  || docker volume create $(MODEL_CACHE_VOLUME)
	@printf '    \033[32m✓\033[0m created outside terraform state\n'

.PHONY: provision
provision: ## Ansible: driver checks, docker, nvidia-container-toolkit, limits
	@cd ansible && $(PLAYBOOK) site.yml $(ANSIBLE_ARGS)

.PHONY: provision-check
provision-check: ## Ansible dry run
	@cd ansible && $(PLAYBOOK) site.yml --check --diff $(ANSIBLE_ARGS)

## --- Model and containers --------------------------------------------------

.PHONY: build
build: ## Build the gateway image (:local)
	@$(COMPOSE) build api

.PHONY: pull-model
pull-model: ## Download MODEL_ID from Hugging Face into the cache volume
	@scripts/pull-model.sh

.PHONY: check-images
check-images: ## Verify the pinned vLLM image tag exists in the registry
	@docker manifest inspect $(VLLM_IMAGE) >/dev/null 2>&1 \
	  && printf '    \033[32m✓\033[0m %s\n' "$(VLLM_IMAGE)" \
	  || { printf '    \033[31m✗\033[0m %s not found - pick a tag from https://hub.docker.com/r/vllm/vllm-openai/tags\n' "$(VLLM_IMAGE)"; exit 1; }

.PHONY: up
up: ## Start the stack
	@$(COMPOSE) $(PROFILES) up -d --remove-orphans
	@$(COMPOSE) ps

.PHONY: down
down: ## Stop the stack (the model cache is untouched)
	@$(COMPOSE) down --remove-orphans

.PHONY: restart
restart: ## Recreate the containers
	@$(COMPOSE) $(PROFILES) up -d --force-recreate

.PHONY: restart-api
restart-api: build ## Rebuild and restart only the gateway
	@$(COMPOSE) up -d --force-recreate --no-deps api

.PHONY: deploy-remote
deploy-remote: ## Ship the repo to the inventory host and deploy over SSH
	@cd ansible && $(PLAYBOOK) deploy.yml -e compose_profiles="$(PROFILES)" $(ANSIBLE_ARGS)

## --- Verification and operations -------------------------------------------

.PHONY: wait
wait: ## Block until the model is loaded and serving
	@scripts/wait-for-ready.sh

.PHONY: show-config
show-config: ## Compare .env vs what Compose passes vs what the engine is running
	@scripts/show-vllm-config.sh

.PHONY: check-auth
check-auth: ## Diagnose a 401: compare .env, the container, and a live request
	@scripts/check-auth.sh

.PHONY: health
health: ## Show gateway liveness and readiness
	@curl -fsS http://127.0.0.1:$(API_PORT)/healthz && echo
	@curl -sS  http://127.0.0.1:$(API_PORT)/readyz  && echo
	@curl -fsS http://127.0.0.1:$(API_PORT)/version && echo

.PHONY: smoke
smoke: ## End-to-end check: auth, completion, streaming, tool calling
	@scripts/smoke-test.sh

.PHONY: logs
logs: ## Follow all logs
	@$(COMPOSE) logs -f --tail 100

.PHONY: logs-vllm
logs-vllm: ## Follow the model server log
	@$(COMPOSE) logs -f --tail 200 vllm

.PHONY: ps
ps: ## Container status
	@$(COMPOSE) ps

.PHONY: gpu
gpu: ## Live GPU utilisation
	@nvidia-smi --query-gpu=index,name,utilization.gpu,memory.used,memory.total,temperature.gpu \
	  --format=csv -l 2

.PHONY: shell
shell: ## Shell inside the gateway container
	@$(COMPOSE) exec api /bin/bash || $(COMPOSE) exec api /bin/sh

.PHONY: vscode-config
vscode-config: ## Print VS Code / Continue configuration for this deployment
	@scripts/vscode-config.sh

.PHONY: vscode-install
vscode-install: ## Write ~/.continue/config.yaml (backs up any existing file)
	@scripts/vscode-config.sh --install

## --- Development -----------------------------------------------------------

.PHONY: venv
venv: ## Create .venv and install the package with dev extras
	@python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)' \
	  || { echo 'python3 >= 3.11 is required'; exit 1; }
	@python3 -m venv .venv
	@$(PY) -m pip install --quiet --upgrade pip
	@$(PY) -m pip install --quiet -e '.[dev]'
	@printf '    \033[32m✓\033[0m .venv ready\n'

.PHONY: test
test: ## Run the unit tests with coverage
	@$(PY) -m pytest --cov --cov-report=term-missing

.PHONY: lint
lint: ## ruff check + format check
	@$(PY) -m ruff check .
	@$(PY) -m ruff format --check .

.PHONY: fmt
fmt: ## Auto-format and auto-fix
	@$(PY) -m ruff check --fix .
	@$(PY) -m ruff format .

.PHONY: typecheck
typecheck: ## mypy --strict
	@$(PY) -m mypy

.PHONY: validate
validate: lint typecheck test tf-validate ## Everything CI runs

.PHONY: tf-validate
tf-validate: ## terraform fmt -check + validate
	@$(TF) fmt -check -recursive
	@$(TF) init -backend=false -input=false >/dev/null && $(TF) validate

.PHONY: ansible-lint
ansible-lint: ## Lint the playbooks (requires ansible-lint)
	@cd ansible && ansible-lint site.yml deploy.yml

.PHONY: run-local
run-local: ## Run the gateway on the host against an already-running vLLM
	@UPSTREAM_BASE_URL=http://127.0.0.1:8999/v1 \
	  $(PY) -m uvicorn llm_assistant_api.main:app --reload --port $(API_PORT)

.PHONY: clean
clean: ## Remove build and test artefacts
	@rm -rf .pytest_cache .mypy_cache .ruff_cache htmlcov .coverage coverage.xml dist build
	@find . -name __pycache__ -type d -prune -exec rm -rf {} + 2>/dev/null || true
	@printf '    \033[32m✓\033[0m cleaned\n'
