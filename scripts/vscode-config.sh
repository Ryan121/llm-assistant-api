#!/usr/bin/env bash
# Emit ready-to-paste VS Code configuration for the running gateway.
#
#   scripts/vscode-config.sh            # print config for every supported extension
#   scripts/vscode-config.sh --install  # also write ~/.continue/config.yaml
set -euo pipefail
source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"

require_env_file

INSTALL=false
[[ "${1:-}" == "--install" ]] && INSTALL=true

MODEL="$(env_get MODEL_ID)"
PORT="$(env_get API_PORT 8081)"
KEY="$(env_get API_KEYS)"; KEY="${KEY%%,*}"
KEY="${KEY:-not-required}"
FIM_MODEL="$(env_get AUTOCOMPLETE_MODEL_ID Qwen/Qwen2.5-Coder-1.5B)"
FIM_ON="$(env_get AUTOCOMPLETE_ENABLED false)"

# The host VS Code should dial. Override for a remote GPU box:
#   GATEWAY_HOST=gpu-01.internal make vscode-config
HOST="${GATEWAY_HOST:-127.0.0.1}"
BASE="http://${HOST}:${PORT}/v1"

# Short label the editor shows in its model picker.
SHORT_NAME="${MODEL##*/}"

OUT="$REPO_ROOT/vscode/continue-config.generated.yaml"
mkdir -p "$REPO_ROOT/vscode"

{
  echo "name: llm-assistant"
  echo "version: 0.1.0"
  echo "schema: v1"
  echo "models:"
  echo "  - name: ${SHORT_NAME}"
  echo "    provider: openai"
  echo "    model: ${MODEL}"
  echo "    apiBase: ${BASE}"
  echo "    apiKey: ${KEY}"
  echo "    roles: [chat, edit, apply]"
  echo "    defaultCompletionOptions:"
  echo "      contextLength: $(env_get MAX_MODEL_LEN 131072)"
  echo "      maxTokens: 8192"
  echo "    capabilities:"
  echo "      - tool_use"
  if [[ "$FIM_ON" == "true" ]]; then
    echo "  - name: ${FIM_MODEL##*/}-autocomplete"
    echo "    provider: openai"
    echo "    model: ${FIM_MODEL}"
    echo "    apiBase: ${BASE}"
    echo "    apiKey: ${KEY}"
    echo "    roles: [autocomplete]"
    echo "    useLegacyCompletionsEndpoint: true"
  fi
  echo "context:"
  echo "  - provider: file"
  echo "  - provider: code"
  echo "  - provider: diff"
  echo "  - provider: terminal"
  echo "  - provider: problems"
  echo "  - provider: codebase"
} >"$OUT"

step "Continue -- RECOMMENDED (chat + edit + agent + autocomplete, no account needed)"
dim "$OUT"
sed 's/^/    /' "$OUT"
echo
info "Install it with:  code --install-extension Continue.continue"
info "Then:             make vscode-install"

if $INSTALL; then
  target="$HOME/.continue/config.yaml"
  if [[ -f "$target" ]]; then
    backup="${target}.bak.$(date +%Y%m%d%H%M%S)"
    cp "$target" "$backup"
    warn "existing config backed up to $backup"
  fi
  mkdir -p "$(dirname "$target")"
  cp "$OUT" "$target"
  ok "installed to $target - reload VS Code to pick it up"
fi

step "Cline / Roo Code / Kilo Code"
info "API Provider ......... OpenAI Compatible"
info "Base URL ............. ${BASE}"
info "API Key .............. ${KEY}"
info "Model ID ............. ${MODEL}"
dim  "Tick 'supports images' off, and enable function/tool calling."

step "Built-in VS Code chat -- only worth it if you already have Copilot"
info "Chat -> model picker -> Manage Models -> OpenAI Compatible"
info "Base URL ${BASE}, key ${KEY}"
dim  "No model picker? It belongs to the GitHub Copilot Chat extension, not to"
dim  "VS Code. Requires that extension plus a signed-in Copilot entitlement,"
dim  "and it replaces the chat model only - completions stay with GitHub."

step "Zed / any OpenAI SDK client"
info "OPENAI_BASE_URL=${BASE}"
info "OPENAI_API_KEY=${KEY}"

echo
dim "Full walkthrough and troubleshooting: docs/VSCODE.md"
