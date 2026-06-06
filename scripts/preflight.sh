#!/usr/bin/env bash
# Поиск плана (по пути или имени) + проверка окружения. Всегда exit 0 (вывод читает Claude).
set -uo pipefail
ARG="${1:-}"
err(){ echo "CIRCLE_PREFLIGHT: ERROR: $*"; exit 0; }

[ -n "$ARG" ] || err "не указан план (имя или путь)"
command -v claude  >/dev/null 2>&1 || err "не найден 'claude' в PATH"
command -v python3 >/dev/null 2>&1 || err "не найден 'python3' (нужен для PTY-раннера)"

if [ -f "$ARG" ]; then
  PLAN="$(cd "$(dirname "$ARG")" && pwd)/$(basename "$ARG")"
else
  MATCHES="$(find . -maxdepth 3 -type f -iname "*$ARG*.md" 2>/dev/null)"
  N="$(printf '%s\n' "$MATCHES" | grep -c . || true)"
  if [ "$N" -eq 0 ]; then err "план не найден ни как путь, ни поиском '*$ARG*.md' в $PWD"; fi
  if [ "$N" -gt 1 ]; then
    echo "CIRCLE_PREFLIGHT: AMBIGUOUS (уточни путь):"; printf '%s\n' "$MATCHES"; exit 0
  fi
  PLAN="$(cd "$(dirname "$MATCHES")" && pwd)/$(basename "$MATCHES")"
fi
[ -f "$PLAN" ] || err "план не найден: $PLAN"

echo "CIRCLE_PREFLIGHT: OK"
echo "PLAN=$PLAN"
echo "WORK=$(dirname "$PLAN")/.circle/$(basename "$PLAN" .md)"
echo "claude=$(command -v claude)  python3=$(command -v python3)"
