#!/usr/bin/env bash
# Shared helpers. Sourced, never executed.

if [[ -t 1 ]]; then
  C_RESET=$'\033[0m'; C_BOLD=$'\033[1m'; C_DIM=$'\033[2m'
  C_RED=$'\033[31m'; C_GREEN=$'\033[32m'; C_YELLOW=$'\033[33m'; C_BLUE=$'\033[34m'
else
  C_RESET=''; C_BOLD=''; C_DIM=''; C_RED=''; C_GREEN=''; C_YELLOW=''; C_BLUE=''
fi

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ENV_FILE="${ENV_FILE:-$REPO_ROOT/.env}"

step() { printf '%s==>%s %s%s%s\n' "$C_BLUE" "$C_RESET" "$C_BOLD" "$*" "$C_RESET"; }
info() { printf '    %s\n' "$*"; }
ok()   { printf '    %s✓%s %s\n' "$C_GREEN" "$C_RESET" "$*"; }
warn() { printf '    %s!%s %s\n' "$C_YELLOW" "$C_RESET" "$*" >&2; }
die()  { printf '%serror:%s %s\n' "$C_RED" "$C_RESET" "$*" >&2; exit 1; }
dim()  { printf '    %s%s%s\n' "$C_DIM" "$*" "$C_RESET"; }

have() { command -v "$1" >/dev/null 2>&1; }

# Read one key from .env without sourcing it (values are never eval'd).
env_get() {
  local key="$1" default="${2-}" value
  [[ -f "$ENV_FILE" ]] || { printf '%s' "$default"; return 0; }
  value="$(grep -E "^[[:space:]]*${key}=" "$ENV_FILE" | tail -1 | cut -d= -f2- || true)"
  value="${value%$'\r'}"
  value="${value#\"}"; value="${value%\"}"
  printf '%s' "${value:-$default}"
}

require_env_file() {
  [[ -f "$ENV_FILE" ]] || die ".env not found. Run 'make env' first."
}

compose() {
  ( cd "$REPO_ROOT" && docker compose "$@" )
}

api_base_url() {
  printf 'http://127.0.0.1:%s' "$(env_get API_PORT 8080)"
}

auth_header() {
  local key
  key="$(env_get API_KEYS)"
  key="${key%%,*}"
  [[ -n "$key" ]] && printf 'Authorization: Bearer %s' "$key"
}
