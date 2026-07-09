#!/usr/bin/env bash
# circle-skill: клиентская утилита телеметрии для проекта — активация/проверка/догон-отправка.
# Вызывается командой /circle-telemetry. ВЫНЕСЕНА в отдельный файл (не inline `!\`bash -c '...'\``
# в md): внутри одинарно-кавыченного `bash -c '...'` литеральные `'` рвали обрамление и скрипт
# исполнялся покорёженным. В обычном файле кавычки работают штатно.
#
# Использование: telemetry-client.sh activate <url> <token> | activate | send
set -uo pipefail

PLUGIN="${CLAUDE_PLUGIN_ROOT:-$(cd "$(dirname "$0")/.." && pwd)}"
PY="${CIRCLE_PYTHON:-python3}"
TELE="$PLUGIN/scripts/circle_telemetry.py"
REPO="$(git rev-parse --show-toplevel 2>/dev/null || pwd)"
ENVF="$REPO/.env"
GI="$REPO/.gitignore"
CMD="${1:-activate}"

env_get(){  # значение ключа $1 из .env (снимаем обрамляющие кавычки, если есть)
  [ -f "$ENVF" ] || return 0
  sed -n "s/^[[:space:]]*$1[[:space:]]*=[[:space:]]*//p" "$ENVF" 2>/dev/null \
    | tail -1 | sed -e 's/^["'\'']//' -e 's/["'\'']$//'
}

env_set(){  # upsert KEY=VAL в .env (замена существующей строки или дозапись)
  touch "$ENVF"; chmod 600 "$ENVF" 2>/dev/null || true
  if grep -qE "^[[:space:]]*$1[[:space:]]*=" "$ENVF" 2>/dev/null; then
    "$PY" - "$ENVF" "$1" "$2" <<'PYEOF'
import re, sys
p, k, v = sys.argv[1:4]
lines = open(p, encoding="utf-8").read().splitlines()
out = [re.sub(r"^\s*" + re.escape(k) + r"\s*=.*$", k + "=" + v, ln) for ln in lines]
open(p, "w", encoding="utf-8").write("\n".join(out) + "\n")
PYEOF
  else
    printf '%s=%s\n' "$1" "$2" >> "$ENVF"
  fi
}

ensure_gitignored(){ grep -qxF '.env' "$GI" 2>/dev/null || printf '.env\n' >> "$GI"; }

case "$CMD" in
  activate)
    if [ -n "${2:-}" ] && [ -n "${3:-}" ]; then   # переданы url token → записать в .env
      ensure_gitignored
      env_set CIRCLE_TELEMETRY_URL "$2"
      env_set CIRCLE_TELEMETRY_TOKEN "$3"
      echo "записаны CIRCLE_TELEMETRY_URL/TOKEN в $ENVF (.env под gitignore)"
    fi
    URL="$(env_get CIRCLE_TELEMETRY_URL)"; TOK="$(env_get CIRCLE_TELEMETRY_TOKEN)"
    if [ -z "$URL" ] || [ -z "$TOK" ]; then
      echo "CIRCLE_TELEMETRY: нет URL/токена в $ENVF — передай: /circle-telemetry activate <url> <token>"
      exit 0
    fi
    CIRCLE_TELEMETRY_TOKEN="$TOK" "$PY" "$TELE" activate --url "$URL"
    ;;
  send)
    URL="$(env_get CIRCLE_TELEMETRY_URL)"; TOK="$(env_get CIRCLE_TELEMETRY_TOKEN)"
    if [ -z "$URL" ] || [ -z "$TOK" ]; then echo "CIRCLE_TELEMETRY: не настроено (нет .env)"; exit 0; fi
    any=0
    for ob in "$REPO"/.circle/*/run-stats/outbox; do
      [ -d "$ob" ] || continue
      W="$(dirname "$(dirname "$ob")")"; any=1
      printf '%s: ' "$(basename "$W")"
      CIRCLE_TELEMETRY_TOKEN="$TOK" "$PY" "$TELE" send --work "$W" --url "$URL"
    done
    [ "$any" = 0 ] && echo "нет накопленных записей (.circle/*/run-stats/outbox пусто)"
    ;;
  *)
    echo "использование: /circle-telemetry activate <url> <token> | activate | send"
    ;;
esac
