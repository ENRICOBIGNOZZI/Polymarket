#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
WRAPPER="$ROOT/CODEX_START_HERE_V7_UNIFICATION.md"
DIRECTIVE="$ROOT/AGENT_DIRECTIVE_V7_UNIFICATION_AND_LEGACY_ERADICATION.md"
LOG_DIR="$ROOT/runs/codex_agent_logs"
STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
LOG_FILE="$LOG_DIR/v7_unification_${STAMP}.log"

cd "$ROOT"

if ! command -v codex >/dev/null 2>&1; then
  echo "codex CLI not found" >&2
  exit 127
fi

for file in "$WRAPPER" "$DIRECTIVE"; do
  if [[ ! -r "$file" ]]; then
    echo "required prompt file not readable: $file" >&2
    exit 2
  fi
done

DIRECTIVE_LINES="$(wc -l < "$DIRECTIVE")"
if (( DIRECTIVE_LINES < 800 )); then
  echo "directive appears truncated: ${DIRECTIVE_LINES} lines" >&2
  exit 3
fi

grep -q '^# 8. FINAL ACCEPTANCE CRITERIA' "$DIRECTIVE"
grep -q '^# 9. FIRST ACTIONS TO EXECUTE NOW' "$DIRECTIVE"

mkdir -p "$LOG_DIR"

printf 'Repository: %s\n' "$ROOT"
printf 'Branch: %s\n' "$(git branch --show-current)"
printf 'HEAD: %s\n' "$(git rev-parse HEAD)"
printf 'Directive lines: %s\n' "$DIRECTIVE_LINES"
printf 'Log: %s\n' "$LOG_FILE"
printf '\nPassing wrapper + COMPLETE directive to Codex...\n\n'

# Current Codex non-interactive mode accepts the complete prompt on stdin via `codex exec -`.
# `workspace-write` lets Codex modify the checked-out repository while preserving a sandbox boundary.
{
  cat "$WRAPPER"
  printf '\n'
  cat "$DIRECTIVE"
} | codex exec --sandbox workspace-write - 2>&1 | tee "$LOG_FILE"
