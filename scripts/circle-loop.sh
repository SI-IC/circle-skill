#!/usr/bin/env bash
# circle-skill: детерминированный цикл. Выбирает фазу, гоняет сессию под PTY,
# сравнивает хеш плана, решает продолжать/стоп. Запускается в фоне.
set -euo pipefail

PLAN_IN="${1:?usage: circle-loop.sh <plan-path>}"
PLAN="$(cd "$(dirname "$PLAN_IN")" && pwd)/$(basename "$PLAN_IN")"
PLUGIN_ROOT="${CLAUDE_PLUGIN_ROOT:-$(cd "$(dirname "$0")/.." && pwd)}"
CLAUDE_BIN="${CIRCLE_CLAUDE_BIN:-claude}"
PY="${CIRCLE_PYTHON:-python3}"
TIMEOUT="${CIRCLE_TIMEOUT:-3600}"
MAX_SAME="${CIRCLE_MAX_SAME_PHASE:-3}"   # backstop: одна фаза подряд > N раз → стоп
PLAN_CLI="$PLUGIN_ROOT/scripts/circle_plan.py"
RUN_PHASE="$PLUGIN_ROOT/scripts/run_phase.py"
TPL="$PLUGIN_ROOT/scripts/executor-prompt.md"

# Рабочая папка — отдельная на каждый план (`.circle/<имя-плана>/`), иначе планы в одной
# директории делили бы loop.log/result/summary и затирали друг друга.
WORK_ROOT="$(dirname "$PLAN")/.circle"
PLAN_SLUG="$(basename "$PLAN" .md)"
WORK="$WORK_ROOT/$PLAN_SLUG"
mkdir -p "$WORK"
LOG="$WORK/loop.log"
RESULT="$WORK/result"
SUMMARY="$WORK/summary.txt"

log(){ printf '%s %s\n' "$(date '+%Y-%m-%dT%H:%M:%S')" "$*" | tee -a "$LOG" >&2; }

# Гарантия: вся .circle/ — вне VCS. Логи фаз несут сырой вывод сессий (возможны
# секреты/чувствительные данные), коммитить их нельзя. `*` в .gitignore покрывает поддерево.
# Fail-closed: не смогли создать .gitignore → не пишем незащищённые логи, останавливаемся.
if ! printf '*\n' > "$WORK_ROOT/.gitignore" 2>/dev/null; then
  log "не удалось создать $WORK_ROOT/.gitignore — отказ писать незащищённые логи, стоп"
  exit 1
fi

# Атомарный портируемый лок (mkdir; flock на macOS нет). PID-файл → сброс залипшего лока.
if ! mkdir "$WORK/lock.d" 2>/dev/null; then
  STALE_PID="$(cat "$WORK/lock.d/pid" 2>/dev/null || true)"
  if [ -n "$STALE_PID" ] && ! kill -0 "$STALE_PID" 2>/dev/null; then
    log "сброс залипшего лока (pid=$STALE_PID мёртв)"
    rm -f "$WORK/lock.d/pid" 2>/dev/null || true
    rmdir "$WORK/lock.d" 2>/dev/null || true
    if ! mkdir "$WORK/lock.d" 2>/dev/null; then
      log "лок перехвачен другим процессом — выход"; exit 1
    fi
  else
    log "цикл уже запущен на этом плане ($WORK/lock.d, pid=${STALE_PID:-?}) — выход"; exit 1
  fi
fi
echo "$$" > "$WORK/lock.d/pid"
cleanup(){ rm -f "$WORK/lock.d/pid" 2>/dev/null || true; rmdir "$WORK/lock.d" 2>/dev/null || true; }
trap cleanup EXIT

unset ANTHROPIC_API_KEY  # гарантия: только подписка, не API

hash_plan(){ "$PY" -c 'import hashlib,sys;print(hashlib.sha256(open(sys.argv[1],"rb").read()).hexdigest())' "$PLAN"; }
# Экранирование для sed: & и \ — спецсимволы replacement-части; | — наш разделитель
# в s|...|...|g (путь с литеральным | иначе сломал бы команду).
sed_escape(){ printf '%s' "$1" | sed 's/[&\\|]/\\&/g'; }

STOP_REASON="complete"
LAST_PHASE=""
SAME_COUNT=0

# Инвариант: circle_plan.py next МОЖЕТ вернуть in_progress-фазу (резюм). Если сессия каждый раз
# меняет план косметически, но не закрывает фазу, hash-гард не срабатывает — поэтому backstop:
# одна и та же фаза выбрана подряд > MAX_SAME раз → стоп (защита от бесконечного расхода).
log "=== старт: $PLAN (timeout=${TIMEOUT}s, max-same=${MAX_SAME}) ==="
while true; do
  if ! NEXT="$("$PY" "$PLAN_CLI" next "$PLAN")"; then
    log "circle_plan.py next вышел с ошибкой — стоп"; STOP_REASON="crash"; break
  fi
  if [ "$NEXT" = "NONE" ]; then
    log "нет подходящих фаз — план исполнен полностью"; STOP_REASON="complete"; break
  fi
  if [[ "$NEXT" != *$'\t'* ]]; then
    log "circle_plan.py next: неожиданный формат '$NEXT' — стоп"; STOP_REASON="crash"; break
  fi
  PHASE_ID="${NEXT%%$'\t'*}"
  PHASE_TITLE="${NEXT#*$'\t'}"

  if [ "$PHASE_ID" = "$LAST_PHASE" ]; then
    SAME_COUNT=$((SAME_COUNT + 1))
  else
    SAME_COUNT=1; LAST_PHASE="$PHASE_ID"
  fi
  if [ "$SAME_COUNT" -gt "$MAX_SAME" ]; then
    log "фаза $PHASE_ID выбрана подряд >$MAX_SAME раз без закрытия — стоп (застряла)"
    STOP_REASON="stuck"; break
  fi

  log "выбрана фаза $PHASE_ID — $PHASE_TITLE (попытка $SAME_COUNT)"

  PLAN_ESC="$(sed_escape "$PLAN")"; WORK_ESC="$(sed_escape "$WORK")"
  PHASE_ID_ESC="$(sed_escape "$PHASE_ID")"; PLAN_CLI_ESC="$(sed_escape "$PLAN_CLI")"
  sed -e "s|@@PLAN@@|$PLAN_ESC|g" -e "s|@@PHASE_ID@@|$PHASE_ID_ESC|g" \
      -e "s|@@WORK@@|$WORK_ESC|g" -e "s|@@PLAN_CLI@@|$PLAN_CLI_ESC|g" \
      "$TPL" > "$WORK/executor-prompt.md"

  # Предсобираем компактный срез плана для фазы (преамбула + текст фазы + журнал),
  # чтобы сессия читала один файл вместо навигации по всему плану.
  if ! "$PY" "$PLAN_CLI" phase-slice "$PLAN" "$PHASE_ID" > "$WORK/phase-context.md"; then
    log "phase-slice фазы $PHASE_ID: ошибка — стоп"; STOP_REASON="crash"; break
  fi

  rm -f "$RESULT"
  if ! H1="$(hash_plan)"; then log "hash_plan до сессии: ошибка — стоп"; STOP_REASON="crash"; break; fi
  START="Прочитай файл $WORK/executor-prompt.md и выполни инструкцию из него полностью, ничего не спрашивая."

  set +e
  "$PY" "$RUN_PHASE" --result "$RESULT" --timeout "$TIMEOUT" --log "$LOG" -- \
        "$CLAUDE_BIN" --dangerously-skip-permissions "$START"
  RC=$?
  set -e
  if ! H2="$(hash_plan)"; then log "hash_plan после сессии: ошибка — стоп"; STOP_REASON="crash"; break; fi

  if [ "$RC" -eq 2 ]; then log "таймаут сессии (${TIMEOUT}s) — стоп"; STOP_REASON="hang"; break; fi
  if [ "$RC" -eq 3 ]; then log "сессия завершилась без result — стоп"; STOP_REASON="crash"; break; fi
  if [ "$RC" -ne 0 ]; then log "run_phase rc=$RC — стоп"; STOP_REASON="error"; break; fi
  if [ "$H1" = "$H2" ]; then log "план не изменился после сессии — стоп (нет прогресса)"; STOP_REASON="no-progress"; break; fi
  log "фаза $PHASE_ID обработана; продолжаю"
done

{
  echo "STOP_REASON=$STOP_REASON"
  echo "---"
  "$PY" "$PLAN_CLI" summary "$PLAN" || true
} > "$SUMMARY"
log "=== стоп: $STOP_REASON. Сводка → $SUMMARY ==="
