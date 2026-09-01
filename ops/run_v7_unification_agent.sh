#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PROMPT="$ROOT/AGENT_DIRECTIVE_V7_UNIFICATION_AND_LEGACY_ERADICATION.md"

if [[ ! -r "$PROMPT" ]]; then
  printf 'prompt not readable: %s\n' "$PROMPT" >&2
  exit 1
fi

if (($# == 0)); then
  exec cat "$PROMPT"
fi

if [[ "$1" == "--stdin" ]]; then
  shift
  if (($# == 0)); then
    printf 'usage: %s --stdin AGENT_COMMAND [ARG ...]\n' "$0" >&2
    exit 2
  fi
  exec "$@" < "$PROMPT"
fi

# Default mode appends the complete directive as one final argument.
# Example: ./ops/run_v7_unification_agent.sh AGENT_COMMAND [ARG ...]
exec "$@" "$(cat "$PROMPT")"
