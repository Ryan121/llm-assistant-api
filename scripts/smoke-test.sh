#!/usr/bin/env bash
# End-to-end check against the running deployment: identity, auth, a real
# completion, streaming, and tool calling (the one that actually matters for
# an agentic coding assistant).
set -euo pipefail
source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"

require_env_file
have curl || die "curl is required"

BASE="$(api_base_url)"
MODEL="$(env_get MODEL_ID)"
AUTH="$(auth_header || true)"

curl_api() {
  local path="$1"; shift
  if [[ -n "$AUTH" ]]; then
    curl -fsS --max-time 180 -H "$AUTH" "$@" "$BASE$path"
  else
    curl -fsS --max-time 180 "$@" "$BASE$path"
  fi
}

failures=0
check() {
  local name="$1"; shift
  if "$@" >/dev/null 2>&1; then ok "$name"; else warn "$name FAILED"; failures=$((failures + 1)); fi
}

step "Identity"
version="$(curl_api /version)" || die "cannot reach $BASE - is the stack up?"
info "$version"

step "Authentication"
if [[ -n "$AUTH" ]]; then
  if curl -fsS --max-time 10 "$BASE/v1/models" >/dev/null 2>&1; then
    warn "an unauthenticated request was ACCEPTED even though API_KEYS is set"
    failures=$((failures + 1))
  else
    ok "unauthenticated requests are rejected"
  fi
  check "authenticated /v1/models" curl_api /v1/models
else
  warn "API_KEYS is empty - the gateway is open to anything that can reach it"
  check "/v1/models" curl_api /v1/models
fi

step "Chat completion"
reply="$(curl_api /v1/chat/completions \
  -H 'content-type: application/json' \
  -d "$(cat <<JSON
{
  "model": "$MODEL",
  "messages": [
    {"role": "system", "content": "You are a terse coding assistant."},
    {"role": "user", "content": "Reply with exactly this word and nothing else: PONG"}
  ],
  "max_tokens": 16,
  "temperature": 0
}
JSON
)")" || { warn "chat completion FAILED"; failures=$((failures + 1)); reply=""; }

if [[ -n "$reply" ]]; then
  content="$(printf '%s' "$reply" | python3 -c \
    'import json,sys; print(json.load(sys.stdin)["choices"][0]["message"]["content"].strip())' \
    2>/dev/null || echo '<unparseable>')"
  info "model said: $content"
  [[ "$content" == *PONG* ]] && ok "round trip through the GPU works" \
    || warn "unexpected content (not fatal - the model just ignored the instruction)"
fi

step "Streaming"
chunks="$(curl_api /v1/chat/completions \
  -H 'content-type: application/json' \
  -N \
  -d "{\"model\":\"$MODEL\",\"messages\":[{\"role\":\"user\",\"content\":\"count to three\"}],\"max_tokens\":32,\"stream\":true}" \
  | grep -c '^data: ' || true)"
if (( chunks > 1 )); then
  ok "received $chunks SSE chunks"
else
  warn "streaming returned $chunks chunks - expected several"
  failures=$((failures + 1))
fi

step "Tool calling (required for agent mode in VS Code)"
tool_reply="$(curl_api /v1/chat/completions \
  -H 'content-type: application/json' \
  -d "$(cat <<JSON
{
  "model": "$MODEL",
  "messages": [{"role": "user", "content": "Read the file src/main.py using the available tool."}],
  "tools": [{
    "type": "function",
    "function": {
      "name": "read_file",
      "description": "Read a file from the workspace",
      "parameters": {
        "type": "object",
        "properties": {"path": {"type": "string"}},
        "required": ["path"]
      }
    }
  }],
  "tool_choice": "auto",
  "max_tokens": 128,
  "temperature": 0
}
JSON
)")" || { warn "tool-calling request FAILED"; failures=$((failures + 1)); tool_reply=""; }

if [[ -n "$tool_reply" ]]; then
  if printf '%s' "$tool_reply" | grep -q '"tool_calls"'; then
    ok "the model emitted a structured tool call"
    printf '%s' "$tool_reply" | python3 -c \
      'import json,sys; c=json.load(sys.stdin)["choices"][0]["message"]["tool_calls"]; print("   ", c[0]["function"])' \
      2>/dev/null || true
  else
    warn "no tool_calls in the response - check VLLM_TOOL_ARGS matches the model family"
    failures=$((failures + 1))
  fi
fi

echo
if (( failures > 0 )); then
  die "$failures check(s) failed."
fi
printf '%sAll smoke tests passed.%s\n' "$C_GREEN" "$C_RESET"
