#!/usr/bin/env bash
# circle-skill: установка приёмника телеметрии как системного systemd-сервиса.
# Демон переживает рестарт контейнера (systemctl enable). Токен живёт ТОЛЬКО в .env
# (через EnvironmentFile), в unit-файл и в VCS не попадает. Идемпотентно.
#
# Использование:  bash scripts/telemetry-server-install.sh
# Требует sudo (системный unit в /etc/systemd/system). Порт 3000, bind 0.0.0.0 —
# проксируется публичным preview-URL контейнера.
set -euo pipefail

REPO="$(cd "$(dirname "$0")/.." && pwd)"
ENV_FILE="$REPO/.env"
STORE="${CIRCLE_TELEMETRY_STORE:-$REPO/.telemetry/runs}"
UNIT=/etc/systemd/system/circle-telemetry.service
PY="$(command -v python3)"

# 1) Токен: генерим сильный, если в .env его ещё нет. Только в .env (gitignored).
touch "$ENV_FILE"; chmod 600 "$ENV_FILE"
if ! grep -qE '^[[:space:]]*CIRCLE_TELEMETRY_TOKEN[[:space:]]*=[[:space:]]*\S' "$ENV_FILE"; then
  TOK="$("$PY" -c 'import secrets; print(secrets.token_urlsafe(32))')"
  printf 'CIRCLE_TELEMETRY_TOKEN=%s\n' "$TOK" >> "$ENV_FILE"
  echo "сгенерирован новый CIRCLE_TELEMETRY_TOKEN → $ENV_FILE"
fi
if ! grep -qE '^[[:space:]]*CIRCLE_TELEMETRY_STORE[[:space:]]*=' "$ENV_FILE"; then
  printf 'CIRCLE_TELEMETRY_STORE=%s\n' "$STORE" >> "$ENV_FILE"
fi
mkdir -p "$STORE"

# 1b) Локальная команда-аналитик /circle-analyze (front-door разбора свежей телеметрии).
# Разворачивается в .claude/commands/ приёмника — gitignored, в пакет плагина НЕ входит, у
# потребителей её нет. Источник scripts/circle-analyze.command.md шипается, но командой не
# становится (команды берутся только из commands/ и .claude/commands/). Filesystem-only, без sudo.
# Идемпотентно: cp перезаписывает — переживает пересоздание контейнера при повторной установке.
CMD_DST="$REPO/.claude/commands"
mkdir -p "$CMD_DST"
cp "$REPO/scripts/circle-analyze.command.md" "$CMD_DST/circle-analyze.md"
echo "команда /circle-analyze развёрнута → $CMD_DST/circle-analyze.md"

# 2) systemd unit. EnvironmentFile=.env → токен вне unit-файла. Restart=always.
sudo tee "$UNIT" >/dev/null <<UNIT_EOF
[Unit]
Description=circle-skill telemetry ingest
After=network.target

[Service]
Type=simple
User=$(whoami)
WorkingDirectory=$REPO
EnvironmentFile=$ENV_FILE
Environment=CIRCLE_TELEMETRY_PORT=3000
Environment=CIRCLE_TELEMETRY_HOST=0.0.0.0
ExecStart=$PY $REPO/scripts/telemetry_server.py
Restart=always
RestartSec=2

[Install]
WantedBy=multi-user.target
UNIT_EOF

# 3) Включить + запустить (enable → автостарт после рестарта контейнера).
sudo systemctl daemon-reload
sudo systemctl enable --now circle-telemetry.service

echo "--- статус ---"
sudo systemctl --no-pager --lines=3 status circle-telemetry.service || true
echo
echo "Токен для проектов (вставь в .env проекта как CIRCLE_TELEMETRY_TOKEN):"
grep -E '^CIRCLE_TELEMETRY_TOKEN=' "$ENV_FILE" | tail -1 | cut -d= -f2-
