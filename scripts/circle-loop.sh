#!/usr/bin/env bash
# circle-skill: детерминированный цикл. Выбирает фазу, гоняет сессию под PTY,
# сравнивает хеш плана, решает продолжать/стоп. Запускается в фоне.
set -euo pipefail

PLAN_IN="${1:?usage: circle-loop.sh <plan-path>}"
PLAN="$(cd "$(dirname "$PLAN_IN")" && pwd)/$(basename "$PLAN_IN")"
PLUGIN_ROOT="${CLAUDE_PLUGIN_ROOT:-$(cd "$(dirname "$0")/.." && pwd)}"
CLAUDE_BIN="${CIRCLE_CLAUDE_BIN:-claude}"
PY="${CIRCLE_PYTHON:-python3}"
# Два дедлайна сессии (см. run_phase.py). IDLE — адаптивный детектор зависания по молчанию
# PTY: живой claude тикает статус-баром даже во время долгого тула, поэтому «тишина дольше idle»
# = завис, а не «фаза долгая». TIMEOUT — абсолютный потолок от runaway; большой, чтобы не рубить
# здоровую-но-долгую фазу. Стоп даёт тот, что сработает раньше.
TIMEOUT="${CIRCLE_TIMEOUT:-14400}"        # абсолютный потолок wall-clock, сек (4ч)
IDLE_TIMEOUT="${CIRCLE_IDLE_TIMEOUT:-1800}"  # обрыв по простою PTY, сек (30мин); 0 = выкл
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
CTX_FILE="$WORK/context-pct"   # run_phase пишет сюда пик контекста сессии, % (для телеметрии)
MISS_FILE="$WORK/manifest-status"  # сессия пишет сюда токен ok/miss(N) — промахи манифеста (телеметрия)

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

# Репозиторий, где сессия фазы коммитит свою работу — тот, что содержит план (там же лежит
# .circle/ с `*`-gitignore, поэтому логи фаз в коммит не попадут). Не git-репо → COMMIT_REPO пуст
# → коммиты выключены. Считаем один раз до цикла.
COMMIT_REPO="$(git -C "$(dirname "$PLAN")" rev-parse --show-toplevel 2>/dev/null || true)"

# Коммитит теперь САМА сессия фазы (последним шагом, после verify/journal), а не цикл: агент,
# столкнувшись с падением pre-commit хука проекта, разбирается и устраняет причину в своём же
# контексте — без обхода (`--no-verify`) и без спавна новой сессии. Цикл лишь СТОРОЖИТ инвариант
# «done ⇒ закоммичено» (commit_guard ниже). `--no-verify` нет нигде — хуки уважаются. Push цикл не
# делает никогда. COMMIT_ENABLED прокидывается в промпт: no → сессии сказано не коммитить, guard off.
if [ -n "${CIRCLE_NO_COMMIT:-}" ] || [ -z "$COMMIT_REPO" ]; then
  COMMIT_ENABLED="no"
else
  COMMIT_ENABLED="yes"
fi

# Identity для коммитов сессии — без затрат её токенов: если имени/почты нет НИГДЕ, экспортим fallback
# в окружение (наследуется сессией через PTY), иначе `git commit` упал бы «Author identity unknown».
# «Есть ли поле?» считаем по фактическому приоритету git: ambient `GIT_AUTHOR/COMMITTER_*` env (если цикл
# запущен с уже выставленной identity) ИЛИ `git config`. Заполняем ТОЛЬКО реально пустое поле — настоящую
# identity (хоть из env, хоть из config, хоть частичную) не перекрываем.
CFG_NAME="$(git -C "$COMMIT_REPO" config user.name 2>/dev/null || true)"
CFG_EMAIL="$(git -C "$COMMIT_REPO" config user.email 2>/dev/null || true)"
HAVE_NAME="${GIT_AUTHOR_NAME:-${GIT_COMMITTER_NAME:-$CFG_NAME}}"
HAVE_EMAIL="${GIT_AUTHOR_EMAIL:-${GIT_COMMITTER_EMAIL:-$CFG_EMAIL}}"
if [ "$COMMIT_ENABLED" = "yes" ] && { [ -z "$HAVE_NAME" ] || [ -z "$HAVE_EMAIL" ]; }; then
  [ -z "$HAVE_NAME" ]  && export GIT_AUTHOR_NAME="circle-skill"        GIT_COMMITTER_NAME="circle-skill"
  [ -z "$HAVE_EMAIL" ] && export GIT_AUTHOR_EMAIL="circle-skill@local" GIT_COMMITTER_EMAIL="circle-skill@local"
  log "git-identity неполная/отсутствует — недостающие поля коммитов фаз заполнит circle-skill@local"
fi

# Телеметрия эффективности (опционально, off по умолчанию). Весь сбор ДЕТЕРМИНИРОВАННЫЙ
# (bash+git+circle_telemetry.py) — ноль лишних токенов сессий; наружу уходит только безопасный
# структурный JSON, отправку сторожит fail-closed скраб на приёмнике. Конфиг — из .env проекта:
# CIRCLE_TELEMETRY_URL + CIRCLE_TELEMETRY_TOKEN (+ опц. CIRCLE_TELEMETRY_SALT для кросс-машинной
# группировки). Нет URL/токена → телеметрия полностью выключена (поведение цикла не меняется).
TELEMETRY="$PLUGIN_ROOT/scripts/circle_telemetry.py"
PROJECT_ENV="${COMMIT_REPO:-$(dirname "$PLAN")}/.env"
# Достаём только наши ключи из .env — без `source` (не исполняем чужой файл).
_env_get(){ [ -f "$PROJECT_ENV" ] && sed -n "s/^[[:space:]]*$1[[:space:]]*=[[:space:]]*//p" "$PROJECT_ENV" 2>/dev/null | tail -1 | sed "s/^[\"']//; s/[\"']$//" || true; }
TELE_URL="${CIRCLE_TELEMETRY_URL:-$(_env_get CIRCLE_TELEMETRY_URL)}"
TELE_TOKEN="${CIRCLE_TELEMETRY_TOKEN:-$(_env_get CIRCLE_TELEMETRY_TOKEN)}"
# Соль НЕ экспортим в окружение (иначе автономная сессия прочла бы её через printenv и могла бы
# брутфорсить низкоэнтропийные HMAC-иды). Передаём инлайн только тем python-вызовам, кому нужна.
TELE_SALT="${CIRCLE_TELEMETRY_SALT:-$(_env_get CIRCLE_TELEMETRY_SALT)}"
TELE_ENABLED="no"
[ -n "$TELE_URL" ] && [ -n "$TELE_TOKEN" ] && TELE_ENABLED="yes"
[ "$TELE_ENABLED" = "yes" ] && log "телеметрия включена (приёмник: $TELE_URL)"

# Стабильный per-run идентификатор и момент старта — персистятся в work-dir и переживают рестарты
# цикла (после hang владелец перезапускает). Один run_uuid на весь прогон → снимки склеиваются на
# приёмнике (overwrite-last), а run_wall_s меряет кумулятивное время прогона, не только последней сессии.
if [ "$TELE_ENABLED" = "yes" ]; then
  mkdir -p "$WORK/run-stats"
  RUN_UUID_FILE="$WORK/run-stats/run_uuid"
  [ -s "$RUN_UUID_FILE" ] || "$PY" -c 'import uuid; print(uuid.uuid4().hex[:12])' > "$RUN_UUID_FILE"
  RUN_UUID="$(cat "$RUN_UUID_FILE")"
  RUN_START_FILE="$WORK/run-stats/run_started"
  [ -s "$RUN_START_FILE" ] || date +%s > "$RUN_START_FILE"
fi

# Сторож инварианта «done ⇒ закоммичено». К этому моменту сессия уже завершилась и должна была
# закоммитить свою работу сама. Признак коммита — СДВИГ HEAD за время сессии ($2 — HEAD до неё), а не
# просто чистота дерева: pre-commit хук-форматтер (`prettier --write` и т. п.) может оставить residue
# уже ПОСЛЕ успешного коммита — дерево грязное, но коммит фазы есть, баунсить нельзя. HEAD не сдвинулся
# и дерево грязное → сессия НЕ закоммитила (забыла/не смогла): НЕ теряем работу и НЕ обходим — возвращаем
# фазу в in_progress с obstacle, цикл переизберёт её, свежая сессия дочинит/докоммитит. Непочиняемый
# случай (хук требует недоступного окружения) ловит backstop MAX_SAME → stuck. `.circle/` гитигнорнута
# и в porcelain не светится. Возврат: 0 — фаза закрыта чисто; 1 — сделан баунс.
commit_guard(){
  local id="$1" head_before="$2" head_after
  [ "$COMMIT_ENABLED" = "yes" ] || return 0
  head_after="$(git -C "$COMMIT_REPO" rev-parse HEAD 2>/dev/null || true)"
  if [ "$head_after" != "$head_before" ]; then
    log "фаза $id закоммичена сессией"; return 0
  fi
  if [ -z "$(git -C "$COMMIT_REPO" status --porcelain 2>/dev/null)" ]; then
    log "фаза $id: коммитить нечего"; return 0
  fi
  log "фаза $id помечена done, но коммита нет — возврат в in_progress (баунс)"
  "$PY" "$PLAN_CLI" set-status "$PLAN" "$id" in_progress \
    --obstacle "фаза была помечена done, но её работа НЕ закоммичена; закоммить свою работу (git add -A && git commit, учти хуки проекта) перед сигналом result" \
    2>>"$LOG" || log "set-status in_progress (баунс) фазы $id: ошибка"
  return 1
}
# Экранирование для sed: & и \ — спецсимволы replacement-части; | — наш разделитель
# в s|...|...|g (путь с литеральным | иначе сломал бы команду).
sed_escape(){ printf '%s' "$1" | sed 's/[&\\|]/\\&/g'; }

STOP_REASON="complete"
LAST_PHASE=""
SAME_COUNT=0
SESSIONS=0  # число запущенных сессий за прогон (для телеметрии; персистится → переживает рестарт,
            # чтобы финальный склеенный снимок нёс кумулятивный счёт, а не только последней инвокации)
SESSIONS_FILE="$WORK/run-stats/sessions"
if [ "$TELE_ENABLED" = "yes" ] && [ -s "$SESSIONS_FILE" ]; then SESSIONS="$(cat "$SESSIONS_FILE")"; fi

# Инвариант: circle_plan.py next МОЖЕТ вернуть in_progress-фазу (резюм). Если сессия каждый раз
# меняет план косметически, но не закрывает фазу, hash-гард не срабатывает — поэтому backstop:
# одна и та же фаза выбрана подряд > MAX_SAME раз → стоп (защита от бесконечного расхода).
log "=== старт: $PLAN (timeout=${TIMEOUT}s, idle=${IDLE_TIMEOUT}s, max-same=${MAX_SAME}) ==="
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
      -e "s|@@COMMIT_ENABLED@@|$COMMIT_ENABLED|g" \
      "$TPL" > "$WORK/executor-prompt.md"

  # Предсобираем компактный срез плана для фазы (преамбула + текст фазы + журнал),
  # чтобы сессия читала один файл вместо навигации по всему плану.
  if ! "$PY" "$PLAN_CLI" phase-slice "$PLAN" "$PHASE_ID" > "$WORK/phase-context.md"; then
    log "phase-slice фазы $PHASE_ID: ошибка — стоп"; STOP_REASON="crash"; break
  fi

  rm -f "$RESULT"
  if ! H1="$(hash_plan)"; then log "hash_plan до сессии: ошибка — стоп"; STOP_REASON="crash"; break; fi
  # HEAD до сессии — по его сдвигу commit_guard узнаёт, закоммитила ли фаза (устойчиво к residue хука).
  HEAD_BEFORE=""
  [ "$COMMIT_ENABLED" = "yes" ] && HEAD_BEFORE="$(git -C "$COMMIT_REPO" rev-parse HEAD 2>/dev/null || true)"
  # Предсессионные замеры телеметрии (детерминированные, best-effort).
  SESSIONS=$((SESSIONS + 1))
  if [ "$TELE_ENABLED" = "yes" ]; then printf '%s\n' "$SESSIONS" > "$SESSIONS_FILE"; fi
  PHASE_START=$(date +%s)
  TELE_HEAD_BEFORE=""; PHASES_BEFORE=0
  if [ "$TELE_ENABLED" = "yes" ]; then
    [ -n "$COMMIT_REPO" ] && TELE_HEAD_BEFORE="$(git -C "$COMMIT_REPO" rev-parse HEAD 2>/dev/null || echo)"
    PHASES_BEFORE="$("$PY" "$PLAN_CLI" phases "$PLAN" 2>/dev/null | wc -l | tr -d ' ' || echo 0)"
  fi
  START="Прочитай файл $WORK/executor-prompt.md и выполни инструкцию из него полностью, ничего не спрашивая."

  set +e
  rm -f "$CTX_FILE" "$MISS_FILE"
  "$PY" "$RUN_PHASE" --result "$RESULT" --timeout "$TIMEOUT" --idle-timeout "$IDLE_TIMEOUT" \
        --context-out "$CTX_FILE" --log "$LOG" --label "фаза $PHASE_ID" -- \
        "$CLAUDE_BIN" --dangerously-skip-permissions "$START"
  RC=$?
  set -e
  if ! H2="$(hash_plan)"; then log "hash_plan после сессии: ошибка — стоп"; STOP_REASON="crash"; break; fi

  if [ "$RC" -eq 2 ]; then log "сессия оборвана по дедлайну (wall=${TIMEOUT}s / idle=${IDLE_TIMEOUT}s; причина — см. CIRCLE_PHASE_END выше) — стоп"; STOP_REASON="hang"; break; fi
  if [ "$RC" -eq 3 ]; then log "сессия завершилась без result — стоп"; STOP_REASON="crash"; break; fi
  if [ "$RC" -ne 0 ]; then log "run_phase rc=$RC — стоп"; STOP_REASON="error"; break; fi
  if [ "$H1" = "$H2" ]; then log "план не изменился после сессии — стоп (нет прогресса)"; STOP_REASON="no-progress"; break; fi
  # Инвариант проверяем только для реально завершённых (done) фаз: коммит должна была сделать
  # сама сессия. Заблокированная/недоделанная фаза откатила свою работу сама — сторожить нечего.
  PHASE_STATUS="$("$PY" "$PLAN_CLI" phases "$PLAN" 2>/dev/null | awk -F'\t' -v id="$PHASE_ID" '$1==id{print $2; exit}')"
  if [ "$PHASE_STATUS" = "done" ]; then
    commit_guard "$PHASE_ID" "$HEAD_BEFORE" || true   # баунс залогирован; зацикливание ловит MAX_SAME
  else
    log "фаза $PHASE_ID: статус=${PHASE_STATUS:-?} (не done)"
  fi

  # Пофазная запись телеметрии — детерминированная, best-effort (сбой не трогает цикл).
  if [ "$TELE_ENABLED" = "yes" ]; then
    PHASE_DUR=$(( $(date +%s) - PHASE_START ))
    AFTER_SHA=""; DIFF_FILES=""
    if [ -n "$COMMIT_REPO" ] && [ -n "$TELE_HEAD_BEFORE" ]; then
      AFTER_SHA="$(git -C "$COMMIT_REPO" rev-parse HEAD 2>/dev/null || echo)"
      if [ -n "$AFTER_SHA" ] && [ "$AFTER_SHA" != "$TELE_HEAD_BEFORE" ]; then
        DIFF_FILES="$(git -C "$COMMIT_REPO" diff --name-only "$TELE_HEAD_BEFORE" "$AFTER_SHA" 2>/dev/null || echo)"
      fi
    fi
    PHASES_AFTER="$("$PY" "$PLAN_CLI" phases "$PLAN" 2>/dev/null | wc -l | tr -d ' ' || echo 0)"
    SUBPHASES=$(( PHASES_AFTER - PHASES_BEFORE )); [ "$SUBPHASES" -lt 0 ] && SUBPHASES=0
    PLAN_CHANGED=0; [ "$H1" != "$H2" ] && PLAN_CHANGED=1
    COMMITTED=0; [ -n "$AFTER_SHA" ] && [ "$AFTER_SHA" != "$TELE_HEAD_BEFORE" ] && COMMITTED=1
    # order + deps_count + autonomy одной командой (одна phases --json, один парс)
    read -r T_ORD T_DEPS T_AUTON <<TELE_META
$("$PY" "$PLAN_CLI" phases "$PLAN" --json 2>/dev/null | "$PY" -c 'import json,sys
try:
    d=json.load(sys.stdin); pid=sys.argv[1]
    p=next((x for x in d if x["id"]==pid), {})
    print(p.get("order",0), len(p.get("deps",[]) or []), p.get("autonomy","auto"))
except Exception:
    print(0,0,"auto")' "$PHASE_ID" 2>/dev/null || echo "0 0 auto")
TELE_META
    T_OUTCOME="${PHASE_STATUS:-error}"
    [ "$PLAN_CHANGED" = 0 ] && [ "$PHASE_STATUS" != "done" ] && T_OUTCOME="no-change"
    CTX_PCT="$(cat "$CTX_FILE" 2>/dev/null || true)"
    # Промахи манифеста: читаем сырой токен (ограниченно — токен крошечный, гигант = мусор),
    # строгий разбор `ok`/`miss(N)`→int делает Python (_parse_miss_count); файла нет → пусто → null.
    MISS_RAW=""
    [ -s "$MISS_FILE" ] && MISS_RAW="$(head -c 64 "$MISS_FILE" 2>/dev/null || true)"
    printf '%s\n' "$DIFF_FILES" | CIRCLE_TELEMETRY_SALT="$TELE_SALT" "$PY" "$TELEMETRY" record-phase "$PLAN" "$PHASE_ID" \
      --work "$WORK" --ordinal "${T_ORD:-0}" --attempts "$SAME_COUNT" --duration-s "$PHASE_DUR" \
      --outcome "$T_OUTCOME" --plan-changed "$PLAN_CHANGED" --committed "$COMMITTED" \
      --deps-count "${T_DEPS:-0}" --autonomy "${T_AUTON:-auto}" --subphases-added "$SUBPHASES" \
      --context-pct "${CTX_PCT:-}" --manifest-misses "${MISS_RAW:-}" \
      2>>"$LOG" || log "телеметрия record-phase фазы $PHASE_ID: ошибка (best-effort)"
  fi
  log "фаза $PHASE_ID обработана; продолжаю"
done

{
  echo "STOP_REASON=$STOP_REASON"
  echo "---"
  "$PY" "$PLAN_CLI" summary "$PLAN" || true
} > "$SUMMARY"

# Финал телеметрии: собрать один conflict-free JSON прогона в outbox и догнать всё неотправленное.
# Всё best-effort; единственная «дорогая» строка — статус отправки (её печатает цикл, не сессия).
if [ "$TELE_ENABLED" = "yes" ]; then
  PLUGIN_VER="$("$PY" -c 'import json,sys;print(json.load(open(sys.argv[1])).get("version","0"))' "$PLUGIN_ROOT/.claude-plugin/plugin.json" 2>/dev/null || echo 0)"
  PHASES_TOTAL="$("$PY" "$PLAN_CLI" phases "$PLAN" 2>/dev/null | wc -l | tr -d ' ' || echo 0)"
  STATUS_CSV="$("$PY" "$PLAN_CLI" summary "$PLAN" --json 2>/dev/null | "$PY" -c 'import json,sys
try:
    c=json.load(sys.stdin).get("counts",{}); print(",".join("%s=%s"%(k,v) for k,v in c.items()))
except Exception:
    print("")' 2>/dev/null || echo)"
  RUN_WALL=$(( $(date +%s) - $(cat "$WORK/run-stats/run_started" 2>/dev/null || date +%s) ))
  [ "$RUN_WALL" -lt 0 ] && RUN_WALL=0
  CIRCLE_TELEMETRY_SALT="$TELE_SALT" "$PY" "$TELEMETRY" build-run "$PLAN" --work "$WORK" --out-dir "$WORK/run-stats/outbox" \
    --plugin-version "$PLUGIN_VER" --stop-reason "$STOP_REASON" \
    --run-wall-s "$RUN_WALL" --run-uuid "${RUN_UUID:-}" --sessions-total "$SESSIONS" --phases-total "$PHASES_TOTAL" \
    --status-counts "$STATUS_CSV" 2>>"$LOG" || log "телеметрия build-run: ошибка (best-effort)"
  # Токен — через env дочернего процесса, НЕ через argv (иначе виден в ps/proc).
  TELE_STATUS="$(CIRCLE_TELEMETRY_TOKEN="$TELE_TOKEN" "$PY" "$TELEMETRY" send --work "$WORK" --url "$TELE_URL" 2>>"$LOG" || echo 'отправлено=0 не_отправлено=? причина=ошибка')"
  log "телеметрия: $TELE_STATUS"
  printf 'телеметрия: %s\n' "$TELE_STATUS" >> "$SUMMARY"
fi

log "=== стоп: $STOP_REASON. Сводка → $SUMMARY ==="
