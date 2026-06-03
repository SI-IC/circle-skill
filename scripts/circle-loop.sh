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
PLAN_CLI="$PLUGIN_ROOT/scripts/circle_plan.py"
RUN_PHASE="$PLUGIN_ROOT/scripts/run_phase.py"
TPL="$PLUGIN_ROOT/scripts/executor-prompt.md"

WORK="$(dirname "$PLAN")/.circle"
mkdir -p "$WORK"
LOG="$WORK/loop.log"
RESULT="$WORK/result"
SUMMARY="$WORK/summary.txt"

log(){ printf '%s %s\n' "$(date '+%H:%M:%S')" "$*" | tee -a "$LOG" >&2; }

# Атомарный портируемый лок (mkdir; flock на macOS нет).
if ! mkdir "$WORK/lock.d" 2>/dev/null; then
  log "цикл уже запущен на этом плане ($WORK/lock.d) — выход"
  exit 1
fi
cleanup(){ rmdir "$WORK/lock.d" 2>/dev/null || true; }
trap cleanup EXIT

unset ANTHROPIC_API_KEY  # гарантия: только подписка, не API

hash_plan(){ "$PY" -c 'import hashlib,sys;print(hashlib.sha256(open(sys.argv[1],"rb").read()).hexdigest())' "$PLAN"; }

STOP_REASON="complete"
log "=== старт: $PLAN (timeout=${TIMEOUT}s) ==="
while true; do
  NEXT="$("$PY" "$PLAN_CLI" next "$PLAN")"
  if [ "$NEXT" = "NONE" ]; then
    log "нет подходящих фаз — план исполнен полностью"; STOP_REASON="complete"; break
  fi
  PHASE_ID="${NEXT%%$'\t'*}"
  PHASE_TITLE="${NEXT#*$'\t'}"
  log "выбрана фаза $PHASE_ID — $PHASE_TITLE"

  sed -e "s|@@PLAN@@|$PLAN|g" -e "s|@@PHASE_ID@@|$PHASE_ID|g" \
      -e "s|@@WORK@@|$WORK|g" -e "s|@@PLAN_CLI@@|$PLAN_CLI|g" \
      "$TPL" > "$WORK/executor-prompt.md"

  rm -f "$RESULT"
  H1="$(hash_plan)"
  START="Прочитай файл $WORK/executor-prompt.md и выполни инструкцию из него полностью, ничего не спрашивая."

  set +e
  "$PY" "$RUN_PHASE" --result "$RESULT" --timeout "$TIMEOUT" --log "$LOG" -- \
        "$CLAUDE_BIN" --dangerously-skip-permissions "$START"
  RC=$?
  set -e
  H2="$(hash_plan)"

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
