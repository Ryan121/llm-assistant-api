#!/usr/bin/env bash
# Diagnose a 401 from the gateway.
#
# A 401 means the token the editor presents does not match API_KEYS. There are
# only three places that can disagree, so check all three rather than guessing:
#
#   1. .env on this host          -- what you intended
#   2. the running api container  -- what the gateway actually loaded
#   3. the editor's own config    -- what gets sent
#
# The container reads API_KEYS once at startup, so editing .env without
# recreating the container leaves the gateway on the old value. That is the
# most common cause by a wide margin.
set -uo pipefail
source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"

require_env_file

# Comparable but not secret, so this output is safe to paste into a ticket.
fingerprint() {
  local token="$1"
  if [[ -z "$token" ]]; then
    printf '<empty>'
  else
    printf '%s... (length %s)' "${token:0:8}" "${#token}"
  fi
}

step "1. API_KEYS in .env"
env_keys="$(env_get API_KEYS)"
if [[ -z "$env_keys" ]]; then
  warn "API_KEYS is empty - the gateway accepts unauthenticated requests."
  warn "A 401 therefore cannot be coming from this deployment's .env."
else
  index=0
  # `|| [[ -n "$key" ]]` is required: the last field has no trailing newline,
  # so read returns non-zero on it and a plain while-read loop skips it
  # entirely -- reporting "0 keys" for a perfectly good single-key .env.
  while IFS= read -r key || [[ -n "$key" ]]; do
    key="${key#"${key%%[![:space:]]*}"}"
    key="${key%"${key##*[![:space:]]}"}"
    [[ -z "$key" ]] && continue
    index=$((index + 1))
    printf '    key %d  %s\n' "$index" "$(fingerprint "$key")"
  done < <(printf '%s' "$env_keys" | tr ',' '\n')

  if (( index == 0 )); then
    warn "API_KEYS is set but contains no usable key - check for stray commas"
  else
    ok "$index key(s) configured; the editor must use key 1"
  fi
fi

step "2. API_KEYS inside the running container"
# `compose ps` exits 0 with empty output when nothing matches, so the exit
# status says nothing about whether the container exists -- test the output.
running="$(compose ps --status running --services 2>/dev/null | grep -Fx api || true)"
container_keys="$(compose exec -T api printenv API_KEYS 2>/dev/null || true)"
container_keys="${container_keys%$'\r'}"
container_keys="${container_keys%$'\n'}"

gateway_is_local=true
if [[ -z "$running" ]]; then
  gateway_is_local=false
  warn "no api container is running on THIS host."
  warn "  If the gateway still answers below, you are reaching it somewhere"
  warn "  else - an SSH tunnel or an editor port forward - and this .env is"
  warn "  not the file it loaded its keys from."
elif [[ -z "$container_keys" ]]; then
  warn "the container reports an EMPTY API_KEYS."
  warn "  Either the stack predates your .env edit, or the value did not reach it."
  info "Fix: make restart-api"
else
  first_container="${container_keys%%,*}"
  printf '    key 1  %s\n' "$(fingerprint "$first_container")"

  first_env="${env_keys%%,*}"
  if [[ "$container_keys" == "$env_keys" ]]; then
    ok "the container matches .env"
  elif [[ "$first_container" == "$first_env" ]]; then
    warn "key 1 matches but the full list differs - a rotation key was added or removed"
    info "Fix (only if you need the new list live): make restart-api"
  else
    warn "MISMATCH: the container is running a different key from .env."
    warn "  The gateway loads API_KEYS once at startup, so your .env edit"
    warn "  has not been applied yet. This is almost certainly your 401."
    info "Fix: make restart-api"
  fi
fi

step "3. End-to-end check against the gateway"
base="$(api_base_url)"

if ! curl -fsS --max-time 5 "$base/healthz" >/dev/null 2>&1; then
  warn "nothing is answering on $base - the stack is down (make ps)"
  echo
  die "cannot reach the gateway"
fi

version="$(curl -fsS --max-time 5 "$base/version" 2>/dev/null || true)"

# Something answering on the port is not proof it is OUR gateway. Without this
# check an unrelated local service on API_PORT produces confident nonsense.
if [[ "$version" != *'"llm-assistant-api"'* ]]; then
  warn "something is listening on $base, but it is not this gateway."
  warn "  /version did not identify as llm-assistant-api. Another service is"
  warn "  holding port $(env_get API_PORT 8081), so the 401 is not ours."
  info "It replied: ${version:-<no /version endpoint>}"
  echo
  die "free the port or change API_PORT in .env"
fi

if [[ "$version" == *'"authenticated":false'* ]]; then
  warn "the gateway reports authentication DISABLED."
  warn "  It will accept any request, so a 401 you are seeing is coming from"
  warn "  somewhere else - a proxy, or a different endpoint than $base."
  echo
  exit 0
fi

anon_code="$(curl -s -o /dev/null -w '%{http_code}' --max-time 10 "$base/v1/models" || true)"
if [[ "$anon_code" == "401" ]]; then
  ok "unauthenticated request correctly rejected (401)"
else
  warn "unauthenticated request returned $anon_code, expected 401"
fi

auth="$(auth_header || true)"
if [[ -z "$auth" ]]; then
  warn "no key in .env to test with"
  echo
  exit 0
fi

auth_code="$(curl -s -o /dev/null -w '%{http_code}' --max-time 10 \
  -H "$auth" "$base/v1/models" || true)"

echo
if [[ "$auth_code" == "200" ]]; then
  printf '%sThe key in .env works against the gateway on %s.%s\n' "$C_GREEN" "$base" "$C_RESET"
  dim "So the mismatch is in your editor. Re-copy it:"
  dim "  make vscode-config      # prints the exact value"
  dim "  make vscode-install     # writes ~/.continue/config.yaml for you"
  dim "Then reload the VS Code window - Continue caches the old config."
elif $gateway_is_local; then
  warn "the key from .env was also rejected ($auth_code)."
  warn "  .env and the running container disagree - see step 2."
  echo
  die "run: make restart-api"
else
  warn "the key from .env was rejected ($auth_code), and no api container runs here."
  warn "  You are talking to a gateway on another host, so THIS .env is not its"
  warn "  source of truth. A key generated by 'make env' on this machine is"
  warn "  unrelated to the one the deployment actually loaded."
  info "It reports itself as: ${version}"
  echo
  info "Fix - on the host that runs the stack:"
  info "    make vscode-config        # prints that deployment's real key"
  info "then paste it into the editor and reload the VS Code window."
  echo
  die "wrong key: this .env belongs to a different host"
fi
