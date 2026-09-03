#!/usr/bin/env bash
# Create .env from the template on first run and mint an API key so the
# gateway is never accidentally left open.
set -euo pipefail
source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"

TEMPLATE="$REPO_ROOT/.env.example"

if [[ -f "$ENV_FILE" ]]; then
  ok ".env already exists - leaving it alone"
else
  step "Creating .env from .env.example"
  cp "$TEMPLATE" "$ENV_FILE"
  chmod 600 "$ENV_FILE"
  ok "wrote $ENV_FILE (mode 600)"
fi

# Only generate a key if the operator has not chosen one.
if [[ -z "$(env_get API_KEYS)" ]]; then
  step "Generating a gateway API key"
  # cut, not `head -c 32`: head closes the pipe as soon as it has its bytes,
  # which can kill tr with SIGPIPE and trip `set -o pipefail`.
  key="sk-local-$(head -c 24 /dev/urandom | base64 | tr -dc 'a-zA-Z0-9' | cut -c1-32)"
  # Portable in-place edit: BSD and GNU sed disagree about -i.
  tmp="$(mktemp)"
  awk -v key="$key" '/^API_KEYS=/ && !done { print "API_KEYS=" key; done=1; next } { print }' \
    "$ENV_FILE" >"$tmp"
  mv "$tmp" "$ENV_FILE"
  chmod 600 "$ENV_FILE"
  ok "API_KEYS set to a freshly generated key"
  dim "$key"
else
  ok "API_KEYS already set"
fi

step "Deployment will use"
info "MODEL_ID              $(env_get MODEL_ID)"
info "TENSOR_PARALLEL_SIZE  $(env_get TENSOR_PARALLEL_SIZE 2)"
info "MAX_MODEL_LEN         $(env_get MAX_MODEL_LEN 131072)"
info "API_PORT              $(env_get API_PORT 8081)"
echo
dim "Edit .env to change the model, then re-run 'make up'."
