---
description: Активировать/проверить отправку телеметрии эффективности circle-skill в приёмник (или догнать неотправленное)
argument-hint: activate <url> <token> | activate | send
allowed-tools: Bash
---

## Действие

!`bash -c '
set -uo pipefail
PLUGIN="${CLAUDE_PLUGIN_ROOT}"
PY="${CIRCLE_PYTHON:-python3}"
TELE="$PLUGIN/scripts/circle_telemetry.py"
REPO="$(git rev-parse --show-toplevel 2>/dev/null || pwd)"
ENVF="$REPO/.env"
GI="$REPO/.gitignore"
CMD="${1:-activate}"

env_get(){ [ -f "$ENVF" ] && sed -n "s/^[[:space:]]*$1[[:space:]]*=[[:space:]]*//p" "$ENVF" 2>/dev/null | tail -1 | sed "s/^[\"'\'']//; s/[\"'\'']$//" || true; }
env_set(){ # upsert KEY=VAL в .env
  touch "$ENVF"; chmod 600 "$ENVF"
  if grep -qE "^[[:space:]]*$1[[:space:]]*=" "$ENVF"; then
    "$PY" - "$ENVF" "$1" "$2" <<PYEOF
import sys,re
p,k,v=sys.argv[1:4]
lines=open(p,encoding="utf-8").read().splitlines()
out=[re.sub(r"^\s*%s\s*=.*$"%re.escape(k), "%s=%s"%(k,v), ln) for ln in lines]
open(p,"w",encoding="utf-8").write("\n".join(out)+"\n")
PYEOF
  else
    printf "%s=%s\n" "$1" "$2" >> "$ENVF"
  fi
}
ensure_gitignored(){ grep -qxE "\\.env" "$GI" 2>/dev/null || printf ".env\n" >> "$GI"; }

if [ "$CMD" = "activate" ]; then
  if [ -n "${2:-}" ] && [ -n "${3:-}" ]; then    # переданы url token → записать в .env
    ensure_gitignored
    env_set CIRCLE_TELEMETRY_URL "$2"
    env_set CIRCLE_TELEMETRY_TOKEN "$3"
    echo "записаны CIRCLE_TELEMETRY_URL/TOKEN в $ENVF (.env под gitignore)"
  fi
  URL="$(env_get CIRCLE_TELEMETRY_URL)"; TOK="$(env_get CIRCLE_TELEMETRY_TOKEN)"
  if [ -z "$URL" ] || [ -z "$TOK" ]; then
    echo "CIRCLE_TELEMETRY: нет URL/токена в $ENVF — передай: /circle-telemetry activate <url> <token>"; exit 0
  fi
  CIRCLE_TELEMETRY_TOKEN="$TOK" "$PY" "$TELE" activate --url "$URL"
elif [ "$CMD" = "send" ]; then
  URL="$(env_get CIRCLE_TELEMETRY_URL)"; TOK="$(env_get CIRCLE_TELEMETRY_TOKEN)"
  if [ -z "$URL" ] || [ -z "$TOK" ]; then echo "CIRCLE_TELEMETRY: не настроено (нет .env)"; exit 0; fi
  any=0
  for ob in "$REPO"/.circle/*/run-stats/outbox; do
    [ -d "$ob" ] || continue
    W="$(dirname "$(dirname "$ob")")"; any=1
    printf "%s: " "$(basename "$W")"
    CIRCLE_TELEMETRY_TOKEN="$TOK" "$PY" "$TELE" send --work "$W" --url "$URL"
  done
  [ "$any" = 0 ] && echo "нет накопленных записей (.circle/*/run-stats/outbox пусто)"
else
  echo "использование: /circle-telemetry activate <url> <token> | activate | send"
fi
' _ $ARGUMENTS`

## Твоя задача

Покажи пользователю результат выполнения выше по-русски одной-двумя строками:
- `активация: … включено` → телеметрия настроена, приёмник отвечает.
- `НЕ включено (причина: …)` → объясни причину (нет связи / неверный токен) и что проверить.
- строки `отправлено=N не_отправлено=M` → сводка догон-отправки.

Ничего не запускай дополнительно — вся работа сделана в блоке выше.
