# CLAUDE.md — circle-skill

Плагин Claude Code: автономное исполнение фазового плана в цикле отдельными интерактивными
сессиями. Обзор и формат плана — в `README.md`. Дизайн и план реализации — в `docs/superpowers/`.

## Архитектура (кратко)

- `commands/circle-skill.md` — препролёт (LLM): поиск плана, нормализация маркеров, риск-классификация,
  подтверждение, фоновый запуск цикла.
- `scripts/circle-loop.sh` — детерминированный цикл: выбор фазы (`circle_plan.py next`) → сессия под
  PTY (`run_phase.py`) → сравнение sha256 плана → стоп/продолжение.
- `scripts/circle_plan.py` — ядро: парсинг маркеров `<!-- circle: ... -->`, выбор фазы, статусы, CLI.
- `scripts/run_phase.py` — запуск интерактивного `claude` под PTY, ожидание `.circle/result`.
- `scripts/executor-prompt.md` — шаблон промпта фазы-исполнителя (@@-плейсхолдеры).

Только подписка, не API: фазы — настоящие интерактивные сессии под PTY, не `claude -p`.

## Правила

- **Версия поднимается при каждом push `main`.** Релизь через `./scripts/release.sh [patch|minor|major]`
  — он поднимает версию в `.claude-plugin/plugin.json` И `.claude-plugin/marketplace.json` (значения обязаны совпадать),
  коммитит, пушит ветку и ставит тег `circle-skill--vX.Y.Z`. Прямой `git push origin main` без поднятой
  версии блокируется хуком `.githooks/pre-push` (обход: `--no-verify`). После клона: `git config core.hooksPath .githooks`.
- **Тесты — stdlib `unittest`, ноль внешних зависимостей.** Прогон: `python3 -m unittest discover -s tests`.
  Меняешь поведение скрипта — добавляй/обновляй тест в том же изменении.
- **Портируемость:** macOS (bash 3.2) + Linux. Без `flock` (лок через `mkdir`), без `jq`/`shasum`-зависимостей
  (хеш и JSON — через `python3`). Не добавляй внешних бинарей.
- **Реальный end-to-end** (живая сессия) тарифицируется и делает реальные изменения — не запускать
  автономно; это ручной шаг владельца (см. «Ручной smoke» в README).
