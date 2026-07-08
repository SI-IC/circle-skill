# Circle Telemetry Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Собирать безопасную (ноль утечки) структурную статистику эффективности пофазных прогонов circle-skill, консолидировать её в приватный git-репо и давать LLM-аналитику данные для предложений по ускорению плагина.

**Architecture:** Новый stdlib-модуль `scripts/circle_telemetry.py` инкапсулирует HMAC-идентификаторы, закрытые словари, fail-closed whitelist-гейт, парс манифеста (только счётчики), staging пофазных записей и финальную сборку одного conflict-free JSON на прогон. `circle-loop.sh` дёргает его детерминированными хуками; `executor-prompt.md` получает опциональный шаг тегирования через CLI-подкоманды без строкового параметра. Всё под env-гейтом с мягкой деградацией.

**Tech Stack:** Python 3 (только stdlib: `argparse`, `json`, `hmac`, `hashlib`, `uuid`, `re`, `os`), bash 3.2, `git`, `unittest`.

## Global Constraints

- Python 3 **только stdlib** — никаких внешних пакетов/бинарей. Без jq, без flock.
- Портируемость: macOS (bash 3.2) + Linux. Хеш/JSON — через `python3`.
- Тесты — stdlib `unittest`. Прогон: `python3 -m unittest discover -s tests`.
- Мягкая деградация: `CIRCLE_TELEMETRY_DIR` не задан → фича полностью off. Best-effort: сбой телеметрии никогда не валит цикл.
- **Ноль утечки — структурно.** В запись не попадают: пути, код, тексты ошибок, содержимое логов, `obstacle`-текст, имена плана/проекта/фаз. Только числа, булевы, enum из закрытого словаря, HMAC-хеши.
- Идентификаторы: `HMAC-SHA256(CIRCLE_TELEMETRY_SALT, value)[:16]`; соль не задана → `"anon"`.
- Меняешь поведение скрипта — обновляй/добавляй тест в том же изменении.
- Дизайн-спека: `docs/superpowers/specs/2026-07-08-circle-telemetry-design.md`.

---

## File Structure

- **Create** `scripts/circle_telemetry.py` — весь телеметрический модуль: константы-словари, `ident()`, гейт-примитивы (`check_enum`, `clamp_int`, `string_ok`), парс манифеста/преамбулы (счётчики), staging (`stat-tag`, `stat-count`, `record-phase`), сборка (`stat-build`), CLI.
- **Create** `tests/test_telemetry.py` — unittest на все pure-функции и CLI-подкоманды.
- **Modify** `scripts/circle-loop.sh` — per-phase замеры (время, BEFORE/AFTER SHA, git diff), вызов `record-phase`, финальный `stat-build` + локальный commit (+ opt-in push).
- **Modify** `scripts/executor-prompt.md` — опциональный шаг самоотчёта под `CIRCLE_TELEMETRY_SELFREPORT`.
- **Modify** `README.md`, `CLAUDE.md` — секция «Телеметрия», env-переменные, приватность, бутстрап.

Существующие тесты лежат в `tests/` (stdlib unittest, поддельный claude для e2e). Новый модуль тестируется изолированно, без живых сессий.

---

### Task 1: HMAC-идентификаторы (`ident`)

**Files:**

- Create: `scripts/circle_telemetry.py`
- Test: `tests/test_telemetry.py`

**Interfaces:**

- Produces: `ident(value: str, salt) -> str` — `HMAC-SHA256(salt, value)` hex, первые 16 символов; `salt` пустой/`None` → `"anon"`. Детерминирована при одной соли (кросс-машинная группировка), необратима без соли.

- [ ] **Step 1: Написать падающий тест**

```python
# tests/test_telemetry.py
import importlib.util, os, unittest
from pathlib import Path

_SPEC = importlib.util.spec_from_file_location(
    "circle_telemetry",
    Path(__file__).resolve().parent.parent / "scripts" / "circle_telemetry.py",
)
tele = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(tele)


class TestIdent(unittest.TestCase):
    def test_stable_with_salt(self):
        a = tele.ident("my-plan", "s3cr3t")
        b = tele.ident("my-plan", "s3cr3t")
        self.assertEqual(a, b)
        self.assertRegex(a, r"\A[0-9a-f]{16}\Z")

    def test_salt_changes_digest(self):
        self.assertNotEqual(tele.ident("my-plan", "salt-A"), tele.ident("my-plan", "salt-B"))

    def test_no_salt_is_anon(self):
        self.assertEqual(tele.ident("my-plan", None), "anon")
        self.assertEqual(tele.ident("my-plan", ""), "anon")

    def test_irreversible_without_salt(self):
        # Голый sha от того же значения НЕ равен HMAC — значит без соли не восстановить.
        import hashlib
        self.assertNotEqual(tele.ident("host1", "salt"), hashlib.sha256(b"host1").hexdigest()[:16])


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Запустить — убедиться, что падает**

Run: `python3 -m unittest tests.test_telemetry -v`
Expected: FAIL — `ModuleNotFoundError` / `AttributeError: module 'circle_telemetry' has no attribute 'ident'` (файла ещё нет).

- [ ] **Step 3: Минимальная реализация**

```python
# scripts/circle_telemetry.py
"""circle-skill: сбор безопасной структурной статистики прогонов.

Гарантия приватности — СТРУКТУРНАЯ: запись собирается из фиксированной схемы,
значения — только числа/булевы/enum из закрытых словарей/HMAC-хеши. Свободного
текста, путей, кода, имён проекта в записи нет по построению. См.
docs/superpowers/specs/2026-07-08-circle-telemetry-design.md.
"""
import hashlib
import hmac


def ident(value: str, salt) -> str:
    """HMAC-SHA256(salt, value), первые 16 hex-символов. Без соли → 'anon'
    (теряем кросс-машинную группировку, но не течём и не даём брутфорс)."""
    if not salt:
        return "anon"
    return hmac.new(
        salt.encode("utf-8"), value.encode("utf-8"), hashlib.sha256
    ).hexdigest()[:16]
```

- [ ] **Step 4: Запустить — убедиться, что проходит**

Run: `python3 -m unittest tests.test_telemetry -v`
Expected: PASS (4 теста класса TestIdent).

- [ ] **Step 5: Commit**

```bash
git add scripts/circle_telemetry.py tests/test_telemetry.py
git commit -m "feat(telemetry): HMAC-идентификаторы с anon-фолбэком"
```

---

### Task 2: Закрытые словари и гейт-примитивы

**Files:**

- Modify: `scripts/circle_telemetry.py`
- Test: `tests/test_telemetry.py`

**Interfaces:**

- Produces:
  - Константы: `SCHEMA_VERSION="1"`, `FRICTION_TAGS` (frozenset из 12 тегов), `VERIFY_GATE_KINDS` (frozenset), `STOP_REASONS` (frozenset), `OUTCOMES` (frozenset), `COUNTERS` (dict `name -> (min,max)`).
  - `check_enum(value: str, vocab) -> str | None` — `value`, если ∈ `vocab`, иначе `None` (поле дропается).
  - `clamp_int(value, lo, hi) -> int` — целое, зажатое в `[lo,hi]`; нецелое/None → `lo`.
  - `string_ok(s: str) -> bool` — `True` только для «безопасной» строки (hex-хеш, enum-токен, semver, `anon`): `re.fullmatch(r"[0-9a-z._-]{1,40}", s)`. Ловит путь (`/`), email (`@`), пробел, кавычку, длинный текст.

- [ ] **Step 1: Написать падающий тест**

```python
# добавить в tests/test_telemetry.py
class TestVocabGate(unittest.TestCase):
    def test_friction_vocab_closed(self):
        self.assertIn("manifest_incomplete", tele.FRICTION_TAGS)
        self.assertIn("journal_stale", tele.FRICTION_TAGS)
        self.assertEqual(len(tele.FRICTION_TAGS), 12)

    def test_check_enum(self):
        self.assertEqual(tele.check_enum("done", tele.OUTCOMES), "done")
        self.assertIsNone(tele.check_enum("rm -rf /", tele.OUTCOMES))
        self.assertIsNone(tele.check_enum("secret-token", tele.FRICTION_TAGS))

    def test_clamp_int(self):
        self.assertEqual(tele.clamp_int(50, 0, 99), 50)
        self.assertEqual(tele.clamp_int(999, 0, 99), 99)
        self.assertEqual(tele.clamp_int(-5, 0, 99), 0)
        self.assertEqual(tele.clamp_int("nan", 0, 99), 0)

    def test_string_ok_rejects_leaks(self):
        self.assertTrue(tele.string_ok("a1b2c3d4e5f60718"))   # hash
        self.assertTrue(tele.string_ok("manifest_stale"))     # tag
        self.assertTrue(tele.string_ok("1.2.3"))              # semver
        self.assertTrue(tele.string_ok("anon"))
        self.assertFalse(tele.string_ok("src/app/secret.py")) # путь
        self.assertFalse(tele.string_ok("user@host.com"))     # email
        self.assertFalse(tele.string_ok("token = ABCDEF"))    # пробел/текст
        self.assertFalse(tele.string_ok('"quoted"'))
```

- [ ] **Step 2: Запустить — убедиться, что падает**

Run: `python3 -m unittest tests.test_telemetry.TestVocabGate -v`
Expected: FAIL — `AttributeError: module 'circle_telemetry' has no attribute 'FRICTION_TAGS'`.

- [ ] **Step 3: Реализация**

```python
# scripts/circle_telemetry.py — добавить под import'ами
import re

SCHEMA_VERSION = "1"

FRICTION_TAGS = frozenset({
    "manifest_incomplete", "manifest_stale", "map_gap", "journal_stale",
    "verify_weak", "phase_too_big", "dep_missing", "blind_search", "rework",
    "preamble_insufficient", "context_slice_insufficient", "full_plan_fallback",
})
VERIFY_GATE_KINDS = frozenset({"none", "typecheck", "test-run", "smoke-exec", "manual"})
STOP_REASONS = frozenset({"complete", "no-progress", "hang", "crash", "error", "stuck"})
OUTCOMES = frozenset({"done", "blocked", "no-change", "skipped", "crash", "error"})
COUNTERS = {"blind_searches": (0, 99)}

_STRING_RE = re.compile(r"[0-9a-z._-]{1,40}")


def check_enum(value, vocab):
    """value, если строка ∈ vocab; иначе None → поле дропается гейтом."""
    return value if isinstance(value, str) and value in vocab else None


def clamp_int(value, lo, hi):
    """Целое, зажатое в [lo,hi]. Нецелое/None → lo. Режет ковровый канал магнитуды."""
    try:
        n = int(value)
    except (TypeError, ValueError):
        return lo
    return max(lo, min(hi, n))


def string_ok(s):
    """True только для безопасной строки (hex/enum/semver/anon). Ловит путь, email,
    пробел, кавычку, длинный текст — бэкстоп поверх типизированной сборки."""
    return isinstance(s, str) and bool(_STRING_RE.fullmatch(s))
```

- [ ] **Step 4: Запустить — убедиться, что проходит**

Run: `python3 -m unittest tests.test_telemetry.TestVocabGate -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add scripts/circle_telemetry.py tests/test_telemetry.py
git commit -m "feat(telemetry): закрытые словари + гейт-примитивы"
```

---

### Task 3: Парс манифеста и преамбулы (только счётчики)

**Files:**

- Modify: `scripts/circle_telemetry.py`
- Test: `tests/test_telemetry.py`

**Interfaces:**

- Produces:
  - `phase_body(text: str, phase_id: str) -> str` — текст тела фазы (от её заголовка `## Фаза <id>` до следующего `## `). Пусто, если не найдена.
  - `manifest_paths(text: str, phase_id: str) -> set[str]` — множество путей из секции «Файловый манифест» тела фазы (backtick-квотированный первый токен каждого буллета). Внутреннее употребление — только для пересечения; наружу уходит лишь `len`.
  - `has_codebase_map(text: str) -> bool` — есть ли в преамбуле секция «Карта кодовой базы».

- [ ] **Step 1: Написать падающий тест**

```python
# добавить в tests/test_telemetry.py
_PLAN = """# План

## Карта кодовой базы
- `scripts/circle_plan.py` — ядро.

## Фаза 1 — Первая
<!-- circle: status=done order=10 deps=[] autonomy=auto obstacle="" -->

### Файловый манифест
- `scripts/circle_plan.py` — CLI (символ `main`).
- `tests/test_plan.py` — тесты.

Текст фазы.

## Фаза 2 — Вторая
<!-- circle: status=pending order=20 deps=[1] autonomy=auto obstacle="" -->

Без манифеста.
"""


class TestParsing(unittest.TestCase):
    def test_manifest_paths_counts_not_leaks(self):
        paths = tele.manifest_paths(_PLAN, "1")
        self.assertEqual(paths, {"scripts/circle_plan.py", "tests/test_plan.py"})

    def test_manifest_absent_is_empty(self):
        self.assertEqual(tele.manifest_paths(_PLAN, "2"), set())

    def test_has_codebase_map(self):
        self.assertTrue(tele.has_codebase_map(_PLAN))
        self.assertFalse(tele.has_codebase_map("# План\n## Фаза 1 — X\n"))
```

- [ ] **Step 2: Запустить — убедиться, что падает**

Run: `python3 -m unittest tests.test_telemetry.TestParsing -v`
Expected: FAIL — `AttributeError: ... 'manifest_paths'`.

- [ ] **Step 3: Реализация**

```python
# scripts/circle_telemetry.py — добавить
_HEADING_RE = re.compile(r"^##\s+Фаза\s+(\S+)\s+—\s+(.+?)\s*$")
_MANIFEST_RE = re.compile(r"файловый манифест", re.IGNORECASE)
_MAP_RE = re.compile(r"^##\s+Карта кодовой базы", re.IGNORECASE | re.MULTILINE)
_BULLET_PATH_RE = re.compile(r"^\s*[-*]\s+`([^`]+)`")


def phase_body(text, phase_id):
    lines = text.splitlines()
    start = None
    for i, ln in enumerate(lines):
        m = _HEADING_RE.match(ln)
        if m and m.group(1) == phase_id:
            start = i + 1
            break
    if start is None:
        return ""
    end = next((j for j in range(start, len(lines)) if lines[j].startswith("## ")), len(lines))
    return "\n".join(lines[start:end])


def manifest_paths(text, phase_id):
    """Множество путей из секции «Файловый манифест» тела фазы. Только для
    пересечения в памяти — наружу уходит лишь количество, пути не сериализуются."""
    body = phase_body(text, phase_id)
    if not body:
        return set()
    lines = body.splitlines()
    sec = next((i for i, ln in enumerate(lines) if ln.startswith("#") and _MANIFEST_RE.search(ln)), None)
    if sec is None:
        return set()
    paths = set()
    for ln in lines[sec + 1:]:
        if ln.startswith("#"):
            break
        m = _BULLET_PATH_RE.match(ln)
        if m:
            paths.add(m.group(1).strip())
    return paths


def has_codebase_map(text):
    return bool(_MAP_RE.search(text))
```

- [ ] **Step 4: Запустить — убедиться, что проходит**

Run: `python3 -m unittest tests.test_telemetry.TestParsing -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add scripts/circle_telemetry.py tests/test_telemetry.py
git commit -m "feat(telemetry): парс манифеста/карты в счётчики (пути не покидают процесс)"
```

---

### Task 4: Канал executor'а — `stat-tag` / `stat-count`

**Files:**

- Modify: `scripts/circle_telemetry.py`
- Test: `tests/test_telemetry.py`

**Interfaces:**

- Produces:
  - `stat_tag(work: str, phase_id: str, tag: str) -> bool` — если `tag` ∈ (`FRICTION_TAGS` ∪ `{"gate:"+k for k in VERIFY_GATE_KINDS}`), дописывает строку в staging `<work>/run-stats/tags-<phase_id>.txt` и возвращает `True`; иначе ничего не пишет, `False`. У функции нет свободного строкового канала — только членство в словаре.
  - `stat_count(work: str, phase_id: str, counter: str, value) -> bool` — если `counter` ∈ `COUNTERS`, дописывает `count:<counter>:<clamped>` и возвращает `True`; иначе `False`.
  - CLI: `stat-tag <plan-unused> <phase_id> <tag> --work <W>` и `stat-count <plan-unused> <phase_id> <counter> <value> --work <W>`. (Сигнатура с `plan` для единообразия вызова из промпта; путь плана не читается этими командами.)

Staging живёт в `.circle/<план>/run-stats/` (под `*`-gitignore) — сырьё executor'а не покидает машину.

- [ ] **Step 1: Написать падающий тест**

```python
# добавить в tests/test_telemetry.py
import tempfile


class TestExecutorChannel(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()

    def test_valid_tag_written(self):
        self.assertTrue(tele.stat_tag(self.tmp, "2", "manifest_incomplete"))
        with open(os.path.join(self.tmp, "run-stats", "tags-2.txt")) as f:
            self.assertIn("manifest_incomplete", f.read())

    def test_unknown_tag_dropped(self):
        self.assertFalse(tele.stat_tag(self.tmp, "2", "leak:/etc/passwd"))
        self.assertFalse(os.path.exists(os.path.join(self.tmp, "run-stats", "tags-2.txt")))

    def test_verify_gate_tag(self):
        self.assertTrue(tele.stat_tag(self.tmp, "2", "gate:smoke-exec"))

    def test_count_clamped(self):
        self.assertTrue(tele.stat_count(self.tmp, "2", "blind_searches", 500))
        with open(os.path.join(self.tmp, "run-stats", "tags-2.txt")) as f:
            self.assertIn("count:blind_searches:99", f.read())

    def test_unknown_counter_dropped(self):
        self.assertFalse(tele.stat_count(self.tmp, "2", "exfiltrate", 1))
```

- [ ] **Step 2: Запустить — убедиться, что падает**

Run: `python3 -m unittest tests.test_telemetry.TestExecutorChannel -v`
Expected: FAIL — `AttributeError: ... 'stat_tag'`.

- [ ] **Step 3: Реализация**

```python
# scripts/circle_telemetry.py — добавить
import os

_GATE_PREFIX = "gate:"


def _tags_path(work, phase_id):
    d = os.path.join(work, "run-stats")
    os.makedirs(d, exist_ok=True)
    return os.path.join(d, "tags-%s.txt" % phase_id)


def _append_line(path, line):
    with open(path, "a", encoding="utf-8") as f:
        f.write(line + "\n")


def stat_tag(work, phase_id, tag):
    """Валидируем ПРИ ЗАПИСИ: tag ∈ FRICTION_TAGS или 'gate:<verify_gate_kind>'.
    Нет свободного строкового канала — только членство в закрытом словаре."""
    if tag in FRICTION_TAGS:
        _append_line(_tags_path(work, phase_id), tag)
        return True
    if tag.startswith(_GATE_PREFIX) and tag[len(_GATE_PREFIX):] in VERIFY_GATE_KINDS:
        _append_line(_tags_path(work, phase_id), tag)
        return True
    return False


def stat_count(work, phase_id, counter, value):
    rng = COUNTERS.get(counter)
    if rng is None:
        return False
    _append_line(_tags_path(work, phase_id), "count:%s:%d" % (counter, clamp_int(value, *rng)))
    return True
```

- [ ] **Step 4: Запустить — убедиться, что проходит**

Run: `python3 -m unittest tests.test_telemetry.TestExecutorChannel -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add scripts/circle_telemetry.py tests/test_telemetry.py
git commit -m "feat(telemetry): канал executor'а stat-tag/stat-count (валидация при записи)"
```

---

### Task 5: `record-phase` — детерминированный скелет фазы

**Files:**

- Modify: `scripts/circle_telemetry.py`
- Test: `tests/test_telemetry.py`

**Interfaces:**

- Produces: `record_phase(work, plan_text, phase_id, *, ordinal, attempts, duration_s, outcome, plan_changed, committed, deps_count, autonomy, subphases_added, touched_paths) -> dict` — считает `files_changed=len(touched_paths)`, `manifest_declared`, `files_off_manifest`, `manifest_declared_untouched`, `coverage_ratio` (пересечение с `manifest_paths` в памяти), `journal_digest_bytes` (длина journal-секции плана в байтах), собирает провалидированный dict скелета и дописывает JSON-строку в `<work>/run-stats/phases.jsonl`. Возвращает собранный dict. `touched_paths` приходит из stdin (git diff), наружу — только счётчики.

- [ ] **Step 1: Написать падающий тест**

```python
# добавить в tests/test_telemetry.py
import json


class TestRecordPhase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()

    def test_manifest_coverage_counts(self):
        rec = tele.record_phase(
            self.tmp, _PLAN, "1",
            ordinal=10, attempts=1, duration_s=42, outcome="done",
            plan_changed=True, committed=True, deps_count=0, autonomy="auto",
            subphases_added=0,
            touched_paths=["scripts/circle_plan.py", "scripts/new_blind.py"],
        )
        self.assertEqual(rec["manifest_declared"], 2)      # объявлено 2
        self.assertEqual(rec["files_changed"], 2)          # тронуто 2
        self.assertEqual(rec["files_off_manifest"], 1)     # new_blind.py вне манифеста
        self.assertEqual(rec["manifest_declared_untouched"], 1)  # test_plan.py не тронут
        self.assertAlmostEqual(rec["coverage_ratio"], 0.5)  # 1 из 2 тронутых в манифесте
        self.assertNotIn("scripts/new_blind.py", json.dumps(rec))  # путь НЕ в записи

    def test_outcome_enum_dropped_if_bad(self):
        rec = tele.record_phase(
            self.tmp, _PLAN, "1", ordinal=10, attempts=1, duration_s=1,
            outcome="rm -rf /", plan_changed=False, committed=False,
            deps_count=0, autonomy="auto", subphases_added=0, touched_paths=[],
        )
        self.assertIsNone(rec.get("outcome"))  # невалидный enum дропнут

    def test_appended_to_staging(self):
        tele.record_phase(
            self.tmp, _PLAN, "1", ordinal=10, attempts=1, duration_s=1,
            outcome="done", plan_changed=True, committed=True,
            deps_count=0, autonomy="auto", subphases_added=0, touched_paths=[],
        )
        with open(os.path.join(self.tmp, "run-stats", "phases.jsonl")) as f:
            self.assertEqual(len(f.read().strip().splitlines()), 1)
```

- [ ] **Step 2: Запустить — убедиться, что падает**

Run: `python3 -m unittest tests.test_telemetry.TestRecordPhase -v`
Expected: FAIL — `AttributeError: ... 'record_phase'`.

- [ ] **Step 3: Реализация**

```python
# scripts/circle_telemetry.py — добавить
import json


def _journal_bytes(plan_text):
    # Дайджест журнала = от «## Журнал» до следующего «## » или конца. Только длина.
    lines = plan_text.splitlines()
    start = next((i + 1 for i, ln in enumerate(lines) if ln.startswith("## Журнал")), None)
    if start is None:
        return 0
    end = next((j for j in range(start, len(lines)) if lines[j].startswith("## ")), len(lines))
    return len("\n".join(lines[start:end]).strip().encode("utf-8"))


def record_phase(work, plan_text, phase_id, *, ordinal, attempts, duration_s, outcome,
                 plan_changed, committed, deps_count, autonomy, subphases_added, touched_paths):
    declared = manifest_paths(plan_text, phase_id)
    touched = set(touched_paths)
    off = touched - declared
    ratio = round(len(touched & declared) / len(touched), 3) if touched else 0.0
    rec = {
        "ordinal": clamp_int(ordinal, 0, 100000),
        "attempts": clamp_int(attempts, 0, 1000),
        "duration_s": clamp_int(duration_s, 0, 10 ** 7),
        "outcome": check_enum(outcome, OUTCOMES),
        "plan_changed": bool(plan_changed),
        "committed": bool(committed),
        "deps_count": clamp_int(deps_count, 0, 1000),
        "autonomy": check_enum(autonomy, {"auto", "needs-human"}),
        "subphases_added": clamp_int(subphases_added, 0, 1000),
        "files_changed": len(touched),
        "manifest_declared": len(declared),
        "files_off_manifest": len(off),
        "manifest_declared_untouched": len(declared - touched),
        "coverage_ratio": ratio,
        "journal_digest_bytes": _journal_bytes(plan_text),
    }
    d = os.path.join(work, "run-stats")
    os.makedirs(d, exist_ok=True)
    _append_line(os.path.join(d, "phases.jsonl"), json.dumps(rec, ensure_ascii=False))
    return rec
```

- [ ] **Step 4: Запустить — убедиться, что проходит**

Run: `python3 -m unittest tests.test_telemetry.TestRecordPhase -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add scripts/circle_telemetry.py tests/test_telemetry.py
git commit -m "feat(telemetry): record-phase — детерминированный скелет фазы"
```

---

### Task 6: `stat-build` — сборка записи прогона + fail-closed гейт + атомная запись

**Files:**

- Modify: `scripts/circle_telemetry.py`
- Test: `tests/test_telemetry.py`

**Interfaces:**

- Produces:
  - `merge_phase_tags(phase_rec: dict, tags_lines: list[str]) -> dict` — добавляет к скелету `friction_tags` (список членов `FRICTION_TAGS`), `verify_gate_kind` (последний `gate:*`), `blind_searches` (из `count:blind_searches:*`); неизвестное игнорирует.
  - `scrub_record(rec) -> bool` — рекурсивно проверяет все строковые значения через `string_ok`; любой провал → `False` (запись дропается).
  - `build_run_record(*, plan_text, plugin_version, machine, plan_slug, salt, stop_reason, run_wall_s, sessions_total, phases_total, status_counts, phase_recs, tags_by_phase) -> dict | None` — собирает run-level + `phases[]` из фикс-схемы, чистит `None`-поля, прогоняет `scrub_record`; провал скраба → `None` (fail-closed).
  - CLI: `stat-build <plan> --work <W> --out <path> --plugin-version <v> --stop-reason <r> --run-wall-s <n> --sessions-total <n> --phases-total <n> --status-counts <k=v,...>` — читает staging, зовёт `build_run_record`, атомарно (`temp`+`os.replace`) пишет `--out`; при `None` не пишет ничего (fail-closed) и печатает `DROPPED` в stderr.

- [ ] **Step 1: Написать падающий тест**

```python
# добавить в tests/test_telemetry.py
class TestStatBuild(unittest.TestCase):
    def test_merge_tags(self):
        merged = tele.merge_phase_tags(
            {"ordinal": 1},
            ["manifest_incomplete", "gate:typecheck", "count:blind_searches:7", "junk"],
        )
        self.assertEqual(merged["friction_tags"], ["manifest_incomplete"])
        self.assertEqual(merged["verify_gate_kind"], "typecheck")
        self.assertEqual(merged["blind_searches"], 7)

    def test_scrub_passes_clean(self):
        self.assertTrue(tele.scrub_record(
            {"plan_id": "a1b2c3d4e5f60718", "outcome": "done", "n": 5, "phases": [{"tag": "rework"}]}
        ))

    def test_scrub_fails_on_leak(self):
        self.assertFalse(tele.scrub_record({"leak": "src/app/secret.py"}))
        self.assertFalse(tele.scrub_record({"phases": [{"path": "user@host"}]}))

    def test_build_fail_closed(self):
        # Внедрённый секрет (через сфабрикованный tag, прошедший бы merge) → скраб роняет всю запись.
        rec = tele.build_run_record(
            plan_text=_PLAN, plugin_version="1.2.3", machine="host", plan_slug="p",
            salt="s", stop_reason="complete", run_wall_s=100, sessions_total=2,
            phases_total=2, status_counts={"done": 2},
            phase_recs=[{"ordinal": 1, "outcome": "done", "leaked": "/etc/passwd"}],
            tags_by_phase={},
        )
        self.assertIsNone(rec)  # fail-closed

    def test_build_ok(self):
        rec = tele.build_run_record(
            plan_text=_PLAN, plugin_version="1.2.3", machine="host", plan_slug="p",
            salt="s", stop_reason="complete", run_wall_s=100, sessions_total=2,
            phases_total=2, status_counts={"done": 2},
            phase_recs=[{"ordinal": 1, "outcome": "done"}], tags_by_phase={},
        )
        self.assertEqual(rec["schema_version"], tele.SCHEMA_VERSION)
        self.assertEqual(rec["stop_reason"], "complete")
        self.assertRegex(rec["plan_id"], r"\A[0-9a-f]{16}\Z")
        self.assertTrue(rec["has_codebase_map"])

    def test_build_drops_bad_stop_reason(self):
        rec = tele.build_run_record(
            plan_text=_PLAN, plugin_version="1.2.3", machine="host", plan_slug="p",
            salt="s", stop_reason="evil; rm", run_wall_s=1, sessions_total=1,
            phases_total=1, status_counts={}, phase_recs=[], tags_by_phase={},
        )
        self.assertIsNone(rec)  # обязательный enum вне словаря → вся запись дропается
```

- [ ] **Step 2: Запустить — убедиться, что падает**

Run: `python3 -m unittest tests.test_telemetry.TestStatBuild -v`
Expected: FAIL — `AttributeError: ... 'merge_phase_tags'`.

- [ ] **Step 3: Реализация**

```python
# scripts/circle_telemetry.py — добавить


def merge_phase_tags(phase_rec, tags_lines):
    friction, gate, blind = [], None, None
    for ln in tags_lines:
        ln = ln.strip()
        if ln in FRICTION_TAGS:
            if ln not in friction:
                friction.append(ln)
        elif ln.startswith(_GATE_PREFIX) and ln[len(_GATE_PREFIX):] in VERIFY_GATE_KINDS:
            gate = ln[len(_GATE_PREFIX):]
        elif ln.startswith("count:blind_searches:"):
            blind = clamp_int(ln.rsplit(":", 1)[-1], *COUNTERS["blind_searches"])
    out = dict(phase_rec)
    if friction:
        out["friction_tags"] = friction
    if gate is not None:
        out["verify_gate_kind"] = gate
    if blind is not None:
        out["blind_searches"] = blind
    return out


def scrub_record(rec):
    """Рекурсивный бэкстоп: любое строковое значение обязано пройти string_ok.
    Провал = баг гейта или инъекция → дроп ВСЕЙ записи (fail-closed)."""
    if isinstance(rec, dict):
        return all(scrub_record(v) for v in rec.values())
    if isinstance(rec, list):
        return all(scrub_record(v) for v in rec)
    if isinstance(rec, str):
        return string_ok(rec)
    if isinstance(rec, bool) or isinstance(rec, (int, float)) or rec is None:
        return True
    return False  # неизвестный тип → дроп


def _drop_none(d):
    return {k: v for k, v in d.items() if v is not None}


def build_run_record(*, plan_text, plugin_version, machine, plan_slug, salt, stop_reason,
                     run_wall_s, sessions_total, phases_total, status_counts,
                     phase_recs, tags_by_phase):
    stop = check_enum(stop_reason, STOP_REASONS)
    if stop is None:                      # обязательный enum вне словаря → fail-closed
        return None
    sc = {k: clamp_int(v, 0, 100000) for k, v in status_counts.items()
          if check_enum(k, {"pending", "in_progress", "done", "blocked", "skipped"})}
    phases = []
    for pr in phase_recs:
        ord_id = str(pr.get("ordinal", ""))
        merged = merge_phase_tags(pr, tags_by_phase.get(ord_id, []))
        phases.append(_drop_none(merged))
    rec = {
        "schema_version": SCHEMA_VERSION,
        "plugin_version": plugin_version if string_ok(plugin_version) else "0",
        "machine_id": ident(machine, salt),
        "plan_id": ident(plan_slug, salt),
        "run_uuid": _run_uuid(),
        "stop_reason": stop,
        "run_wall_s": clamp_int(run_wall_s, 0, 10 ** 8),
        "sessions_total": clamp_int(sessions_total, 0, 100000),
        "phases_total": clamp_int(phases_total, 0, 100000),
        "status_counts": sc,
        "has_codebase_map": has_codebase_map(plan_text),
        "phases": phases,
    }
    return rec if scrub_record(rec) else None


def _run_uuid():
    import uuid
    return uuid.uuid4().hex[:12]
```

- [ ] **Step 4: Запустить — убедиться, что проходит**

Run: `python3 -m unittest tests.test_telemetry.TestStatBuild -v`
Expected: PASS. (Примечание: `run_uuid` 12 hex-символов; `string_ok` допускает до 40 — проходит скраб.)

- [ ] **Step 5: Добавить CLI и тест атомной записи**

```python
# scripts/circle_telemetry.py — добавить main()
def _load_staging(work):
    d = os.path.join(work, "run-stats")
    phase_recs, tags_by_phase = [], {}
    pj = os.path.join(d, "phases.jsonl")
    if os.path.exists(pj):
        with open(pj, encoding="utf-8") as f:
            for ln in f:
                ln = ln.strip()
                if ln:
                    try:
                        pr = json.loads(ln)
                    except ValueError:
                        continue
                    phase_recs.append(pr)
                    tags_by_phase.setdefault(str(pr.get("ordinal", "")), [])
    if os.path.isdir(d):
        for name in os.listdir(d):
            if name.startswith("tags-") and name.endswith(".txt"):
                with open(os.path.join(d, name), encoding="utf-8") as f:
                    lines = [x.strip() for x in f if x.strip()]
                # tags-файлы по phase_id; привязка к ordinal — по порядку в phases.jsonl не нужна:
                # объединяем по всем, merge_phase_tags возьмёт только валидное для каждой фазы.
                for pr in phase_recs:
                    key = str(pr.get("ordinal", ""))
                    tags_by_phase.setdefault(key, []).extend(lines) if name == "tags-%s.txt" % pr.get("_pid", "") else None
    return phase_recs, tags_by_phase


def _atomic_write(path, data):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(data)
    os.replace(tmp, path)


def main(argv=None):
    import argparse
    import sys
    ap = argparse.ArgumentParser(prog="circle_telemetry")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("stat-tag")
    p.add_argument("plan"); p.add_argument("phase_id"); p.add_argument("tag")
    p.add_argument("--work", required=True)

    p = sub.add_parser("stat-count")
    p.add_argument("plan"); p.add_argument("phase_id"); p.add_argument("counter")
    p.add_argument("value"); p.add_argument("--work", required=True)

    p = sub.add_parser("stat-build")
    p.add_argument("plan"); p.add_argument("--work", required=True); p.add_argument("--out", required=True)
    p.add_argument("--plugin-version", default="0"); p.add_argument("--stop-reason", required=True)
    p.add_argument("--run-wall-s", type=int, default=0); p.add_argument("--sessions-total", type=int, default=0)
    p.add_argument("--phases-total", type=int, default=0); p.add_argument("--status-counts", default="")

    a = ap.parse_args(argv)
    salt = os.environ.get("CIRCLE_TELEMETRY_SALT") or None

    if a.cmd == "stat-tag":
        return 0 if stat_tag(a.work, a.phase_id, a.tag) else 0  # best-effort: невалид молча дропнут
    if a.cmd == "stat-count":
        return 0 if stat_count(a.work, a.phase_id, a.counter, a.value) else 0
    if a.cmd == "stat-build":
        with open(a.plan, encoding="utf-8") as f:
            plan_text = f.read()
        phase_recs, tags_by_phase = _load_staging(a.work)
        counts = {}
        for kv in a.status_counts.split(","):
            if "=" in kv:
                k, v = kv.split("=", 1)
                counts[k.strip()] = v.strip()
        import socket
        rec = build_run_record(
            plan_text=plan_text, plugin_version=a.plugin_version, machine=socket.gethostname(),
            plan_slug=os.path.basename(a.plan), salt=salt, stop_reason=a.stop_reason,
            run_wall_s=a.run_wall_s, sessions_total=a.sessions_total, phases_total=a.phases_total,
            status_counts=counts, phase_recs=phase_recs, tags_by_phase=tags_by_phase,
        )
        if rec is None:
            print("DROPPED", file=sys.stderr)
            return 0
        _atomic_write(a.out, json.dumps(rec, ensure_ascii=False, indent=2))
        return 0
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
```

Затем в тест-класс `TestStatBuild` добавить проверку атомной записи через CLI:

```python
    def test_cli_stat_build_writes_file(self):
        import subprocess, sys, pathlib
        tmp = tempfile.mkdtemp()
        plan = os.path.join(tmp, "myplan.md")
        with open(plan, "w") as f:
            f.write(_PLAN)
        work = os.path.join(tmp, "work"); os.makedirs(work)
        # staging: одна фаза
        tele.record_phase(work, _PLAN, "1", ordinal=10, attempts=1, duration_s=5,
                          outcome="done", plan_changed=True, committed=True, deps_count=0,
                          autonomy="auto", subphases_added=0, touched_paths=["scripts/circle_plan.py"])
        out = os.path.join(tmp, "run.json")
        script = pathlib.Path(__file__).resolve().parent.parent / "scripts" / "circle_telemetry.py"
        r = subprocess.run([sys.executable, str(script), "stat-build", plan,
                            "--work", work, "--out", out, "--stop-reason", "complete",
                            "--run-wall-s", "10", "--phases-total", "2"],
                           env={**os.environ, "CIRCLE_TELEMETRY_SALT": "s"}, capture_output=True)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertTrue(os.path.exists(out))
        with open(out) as f:
            data = json.load(f)
        self.assertEqual(data["stop_reason"], "complete")
        self.assertEqual(len(data["phases"]), 1)
```

- [ ] **Step 6: Запустить весь модуль тестов**

Run: `python3 -m unittest tests.test_telemetry -v`
Expected: PASS (все классы).

> **Примечание реализатору по `_load_staging`:** приведённая привязка tags→phase по `_pid` — эскиз. Реализуй так: `record_phase` дополнительно сохраняет в скелет служебное поле `"_pid": phase_id` (не идёт в финальную запись — фильтруется в `build_run_record` через `_drop_none`/явный `pop`), а `_load_staging` группирует `tags-<pid>.txt` по этому `_pid`. Добавь юнит-тест, что tag конкретной фазы попал именно в её запись (две фазы, разные теги). Убедись, что `_pid` НЕ присутствует в финальном JSON (`assertNotIn('_pid', json.dumps(data))`).

- [ ] **Step 7: Commit**

```bash
git add scripts/circle_telemetry.py tests/test_telemetry.py
git commit -m "feat(telemetry): stat-build — сборка записи, fail-closed гейт, атомная запись"
```

---

### Task 7: Врезка в `circle-loop.sh`

**Files:**

- Modify: `scripts/circle-loop.sh` (объявления ~13-15, тело цикла ~118-165, финальный блок ~162-166)
- Test: ручной smoke + существующий e2e-харнесс

**Interfaces:**

- Consumes: `circle_telemetry.py record-phase` / `stat-build` (Task 5, 6), переменные цикла `WORK`, `PLAN`, `COMMIT_REPO`, `SAME_COUNT`, `PHASE_STATUS`, `RC`, `H1`/`H2`.
- Produces: staging в `$WORK/run-stats/`, финальный файл в `$CIRCLE_TELEMETRY_DIR/runs/`.

- [ ] **Step 1: Добавить путь к скрипту и env-гейт (после строки 15)**

```bash
TELEMETRY="$PLUGIN_ROOT/scripts/circle_telemetry.py"
TELE_DIR="${CIRCLE_TELEMETRY_DIR:-}"   # не задан → телеметрия off
```

- [ ] **Step 2: Замер времени и SHA вокруг сессии.** Перед строкой `rm -f "$RESULT"` (сразу после `phase-slice`) добавить:

```bash
  PHASE_START_TS=$(date +%s)
  BEFORE_SHA="-"
  [ -n "$COMMIT_REPO" ] && BEFORE_SHA="$(git -C "$COMMIT_REPO" rev-parse HEAD 2>/dev/null || echo -)"
  PHASES_BEFORE="$("$PY" "$PLAN_CLI" phases "$PLAN" 2>/dev/null | wc -l | tr -d ' ')"
```

- [ ] **Step 3: После вычисления `PHASE_STATUS` (строка ~152, ветка `done`/иначе), перед `log "фаза $PHASE_ID обработана"`, добавить вызов `record-phase` под env-гейтом:**

```bash
  if [ -n "$TELE_DIR" ]; then
    PHASE_DUR=$(( $(date +%s) - PHASE_START_TS ))
    AFTER_SHA="-"; DIFF_FILES=""
    if [ -n "$COMMIT_REPO" ] && [ "$BEFORE_SHA" != "-" ]; then
      AFTER_SHA="$(git -C "$COMMIT_REPO" rev-parse HEAD 2>/dev/null || echo -)"
      DIFF_FILES="$(git -C "$COMMIT_REPO" diff --name-only "$BEFORE_SHA" "$AFTER_SHA" 2>/dev/null)"
    fi
    PHASES_AFTER="$("$PY" "$PLAN_CLI" phases "$PLAN" 2>/dev/null | wc -l | tr -d ' ')"
    SUBPHASES=$(( PHASES_AFTER - PHASES_BEFORE )); [ "$SUBPHASES" -lt 0 ] && SUBPHASES=0
    PLAN_CHANGED=0; [ "$H1" != "$H2" ] && PLAN_CHANGED=1
    COMMITTED=0; [ "$AFTER_SHA" != "$BEFORE_SHA" ] && [ "$AFTER_SHA" != "-" ] && COMMITTED=1
    ORD="$("$PY" "$PLAN_CLI" phases "$PLAN" --json 2>/dev/null \
          | "$PY" -c 'import json,sys;d=json.load(sys.stdin);pid=sys.argv[1];print(next((p.get("order",0) for p in d if p["id"]==pid),0))' "$PHASE_ID" 2>/dev/null || echo 0)"
    DEPS_N="$("$PY" "$PLAN_CLI" phases "$PLAN" --json 2>/dev/null \
          | "$PY" -c 'import json,sys;d=json.load(sys.stdin);pid=sys.argv[1];print(len(next((p.get("deps",[]) for p in d if p["id"]==pid),[])))' "$PHASE_ID" 2>/dev/null || echo 0)"
    OUTCOME="$PHASE_STATUS"; [ "$PLAN_CHANGED" = 0 ] && [ "$PHASE_STATUS" != "done" ] && OUTCOME="no-change"
    printf '%s\n' "$DIFF_FILES" | "$PY" "$TELEMETRY" record-phase "$PLAN" "$PHASE_ID" \
      --work "$WORK" --ordinal "$ORD" --attempts "$SAME_COUNT" --duration-s "$PHASE_DUR" \
      --outcome "$OUTCOME" --plan-changed "$PLAN_CHANGED" --committed "$COMMITTED" \
      --deps-count "$DEPS_N" --autonomy auto --subphases-added "$SUBPHASES" 2>>"$LOG" || true
  fi
```

> **Примечание:** `record-phase` CLI в Task 5/6 добавляет одноимённый subparser, читающий `touched_paths` из stdin (по строке на путь), а `--autonomy` берётся детерминированно; если нужна точная autonomy — прокинь её тем же json-однострочником, что `ORD`/`DEPS_N`. Аргументы `--plan-changed`/`--committed` парсить как `int` (0/1) и приводить к bool в `record_phase`.

- [ ] **Step 4: Финальный `stat-build` + локальный commit.** В хвостовом блоке (после генерации `$SUMMARY`, строки ~162-166) добавить:

```bash
if [ -n "$TELE_DIR" ]; then
  PLUGIN_VER="$("$PY" -c 'import json,sys;print(json.load(open(sys.argv[1])).get("version","0"))' "$PLUGIN_ROOT/.claude-plugin/plugin.json" 2>/dev/null || echo 0)"
  STATUS_CSV="$("$PY" "$PLAN_CLI" summary "$PLAN" --json 2>/dev/null \
      | "$PY" -c 'import json,sys;c=json.load(sys.stdin).get("counts",{});print(",".join(f"{k}={v}" for k,v in c.items()))' 2>/dev/null || echo '')"
  mkdir -p "$TELE_DIR/runs"
  RUN_OUT="$TELE_DIR/runs/$("$PY" -c 'import uuid;print(uuid.uuid4().hex[:12])').json"
  "$PY" "$TELEMETRY" stat-build "$PLAN" --work "$WORK" --out "$RUN_OUT" \
      --plugin-version "$PLUGIN_VER" --stop-reason "$STOP_REASON" \
      --status-counts "$STATUS_CSV" 2>>"$LOG" || log "stat-build: ошибка (телеметрия best-effort)"
  if [ -f "$RUN_OUT" ]; then
    git -C "$TELE_DIR" add "$RUN_OUT" 2>>"$LOG" \
      && git -C "$TELE_DIR" commit -q -m "circle-stats: run" 2>>"$LOG" \
      && log "телеметрия прогона записана" || log "телеметрия: локальный commit не удался (best-effort)"
    if [ -n "${CIRCLE_TELEMETRY_PUSH:-}" ]; then
      git -C "$TELE_DIR" push -q 2>>"$LOG" || log "телеметрия: push не удался (best-effort)"
    fi
  fi
fi
```

> Имя файла собери из `<machine_id>-<plan_id>-<uuid>` внутри `stat-build` вместо голого uuid в bash (машина/план — HMAC-хеши). Проще: пусть `stat-build` сам печатает имя выбранного файла в stdout и пишет в `$TELE_DIR/runs/`, а `--out` замени на `--out-dir`. Реализатор: перенеси генерацию имени в `stat-build` (у него уже есть `machine`/`plan_slug`/`salt` → `ident`), bash передаёт только `--out-dir "$TELE_DIR/runs"`.

- [ ] **Step 5: Прогон существующего e2e-харнесса без телеметрии (регресс)**

Run: `python3 -m unittest discover -s tests -v`
Expected: PASS — телеметрия off по умолчанию (`CIRCLE_TELEMETRY_DIR` не задан), поведение цикла не изменилось.

- [ ] **Step 6: Ручной smoke телеметрии (изолированно, поддельный claude из e2e).** Прогнать e2e-сценарий «complete» с `CIRCLE_TELEMETRY_DIR=$(mktemp -d)` и `CIRCLE_TELEMETRY_SALT=test`, убедиться, что в `runs/` появился один валидный JSON, `python3 -c 'import json;json.load(open(...))'` парсится, полей с путями/именами нет.

- [ ] **Step 7: Commit**

```bash
git add scripts/circle-loop.sh
git commit -m "feat(telemetry): врезка сбора в цикл (record-phase + stat-build, best-effort)"
```

---

### Task 8: Опциональный шаг самоотчёта в `executor-prompt.md`

**Files:**

- Modify: `scripts/executor-prompt.md`, `scripts/circle-loop.sh` (проброс флага в срез)

**Interfaces:**

- Consumes: `stat-tag`/`stat-count` CLI (Task 4).
- Produces: executor помечает трение тегами только при `CIRCLE_TELEMETRY_SELFREPORT`.

- [ ] **Step 1: Добавить в `executor-prompt.md` шаг 5b (после шага 5 «журнал», перед шагом 6 «result»)**

```markdown
5b. **Опциональный самоотчёт (если в срезе есть блок «Самоотчёт: включён»).** Отметь трение,
которое замедлило фазу, — строго тегами из закрытого списка, без свободного текста:
`python3 @@TELEMETRY@@ stat-tag @@PLAN@@ @@PHASE_ID@@ <tag> --work @@WORK@@`
Допустимые `<tag>`: `manifest_incomplete` `manifest_stale` `map_gap` `journal_stale`
`verify_weak` `phase_too_big` `dep_missing` `blind_search` `rework` `preamble_insufficient`
`context_slice_insufficient` `full_plan_fallback`. Тип verify-гейта: `gate:none|typecheck|test-run|smoke-exec|manual`.
Число слепых поисков по репо: `python3 @@TELEMETRY@@ stat-count @@PLAN@@ @@PHASE_ID@@ blind_searches <n> --work @@WORK@@`.
Неизвестный тег молча игнорируется. Ничего, кроме этих команд, в телеметрию не пиши.
```

- [ ] **Step 2: Добавить плейсхолдер `@@TELEMETRY@@` в подстановку `circle-loop.sh`.** В блоке `sed -e ...` (строки ~124-127) добавить `-e "s|@@TELEMETRY@@|$(sed_escape "$TELEMETRY")|g"`, а условную строку «Самоотчёт: включён» вставлять в `phase-context.md` после его генерации, если задан `CIRCLE_TELEMETRY_SELFREPORT`:

```bash
  if [ -n "${CIRCLE_TELEMETRY_SELFREPORT:-}" ]; then
    printf '\n> Самоотчёт: включён\n' >> "$WORK/phase-context.md"
  fi
```

- [ ] **Step 3: Прогон тестов (регресс промпта-шаблона, если есть тест на плейсхолдеры)**

Run: `python3 -m unittest discover -s tests -v`
Expected: PASS.

- [ ] **Step 4: Commit**

```bash
git add scripts/executor-prompt.md scripts/circle-loop.sh
git commit -m "feat(telemetry): опциональный самоотчёт executor'а под CIRCLE_TELEMETRY_SELFREPORT"
```

---

### Task 9: Документация

**Files:**

- Modify: `README.md` (секция после «Настройки (env)»), `CLAUDE.md` (блок «Архитектура»)

**Interfaces:** нет кода; синхронизация доков с поведением в том же изменении (правило проекта).

- [ ] **Step 1: Добавить в `README.md` секцию «Телеметрия»** — назначение (LLM-аналитик, не дашборд), гарантии приватности (структурная схема, HMAC-соль вне репо, ноль свободного текста), бутстрап (создать приватный репо, клонировать, задать env), и таблицу env:

```markdown
## Телеметрия эффективности (опционально)

Цикл может писать безопасную структурную статистику каждого прогона для последующего
LLM-анализа «где план тормозит и как ускорить плагин». По умолчанию **off**.

Гарантия приватности — структурная: в запись попадают только числа, булевы, enum из закрытого
словаря и HMAC-хеши. Ни путей, ни кода, ни текста ошибок, ни `obstacle`, ни имён проекта/фаз —
их там нет по построению схемы (см. `docs/superpowers/specs/2026-07-08-circle-telemetry-design.md`).

Бутстрап: создай приватный git-репо статистики, клонируй на каждой машине, задай env:

| Env                           | Дефолт           | Смысл                                                           |
| ----------------------------- | ---------------- | --------------------------------------------------------------- |
| `CIRCLE_TELEMETRY_DIR`        | off              | локальный клон приватного репо телеметрии                       |
| `CIRCLE_TELEMETRY_SALT`       | `anon`           | секрет HMAC идентификаторов; вне репо, одинаков на всех машинах |
| `CIRCLE_TELEMETRY_PUSH`       | локальный commit | `1` → best-effort push в конце прогона                          |
| `CIRCLE_TELEMETRY_SELFREPORT` | off              | включает опциональные friction-теги от фазы-исполнителя         |

Анализ: на любой машине `git pull` репо статистики и попроси Claude разобрать `runs/*.json`.
```

- [ ] **Step 2: Добавить в `CLAUDE.md` (блок «Архитектура (кратко)») строку про `circle_telemetry.py`:**

```markdown
- `scripts/circle_telemetry.py` — опциональная телеметрия прогонов (off по умолчанию): HMAC-идентификаторы,
  закрытые словари, fail-closed whitelist-гейт, парс манифеста в счётчики (пути не покидают процесс),
  staging + сборка одного conflict-free JSON на прогон в `CIRCLE_TELEMETRY_DIR`. Потребитель — LLM-аналитик.
```

- [ ] **Step 3: Grep-проверка синхронности** (нет упоминаний несуществующих флагов):

Run: `grep -rn "CIRCLE_TELEMETRY" README.md CLAUDE.md scripts/`
Expected: env-имена совпадают везде (`_DIR`, `_SALT`, `_PUSH`, `_SELFREPORT`).

- [ ] **Step 4: Commit**

```bash
git add README.md CLAUDE.md
git commit -m "docs: секция телеметрии эффективности (env, приватность, бутстрап)"
```

---

## Self-Review (заполнено автором плана)

**Spec coverage:**

- Расщепление доверия (детерм. скелет + опц. самоотчёт) → Task 5 (скелет), Task 4/8 (самоотчёт). ✔
- Ноль свободного текста / whitelist-схема / fail-closed → Task 2 (примитивы), Task 6 (`build_run_record`/`scrub_record`). ✔
- HMAC-идентификаторы, `anon` без соли → Task 1. ✔
- Покрытие манифеста из git-дифа (главный сигнал) → Task 3 (парс) + Task 5 (пересечение) + Task 7 (git diff в stdin). ✔
- Канал executor'а как CLI без строкового параметра → Task 4. ✔
- Консолидация: один conflict-free файл, локальный commit, push opt-in → Task 6 (имя из хешей+uuid) + Task 7. ✔
- Env мягкая деградация → Task 7 (гейт `TELE_DIR`), Task 1/6 (`anon`). ✔
- Аналитический workflow, схема записи, словари → Task 2 (словари), Task 6 (сборка). ✔
- Затрагиваемые компоненты + env-таблица + тесты → Task 9 (доки), тест-классы в каждой задаче. ✔

**Placeholder scan:** код приведён целиком в каждом шаге. Единственная явно помеченная «эскизная» точка — привязка tags→phase в `_load_staging` (Task 6, Step 5/6) — снабжена конкретным указанием реализатору (служебное `_pid`-поле + тест изоляции) и не является «TODO: fill in».

**Type consistency:** имена функций и сигнатуры сверены между задачами: `ident`, `check_enum`, `clamp_int`, `string_ok` (Task 2) → используются в Task 5/6; `manifest_paths`/`has_codebase_map` (Task 3) → Task 5/6; `record_phase` (Task 5) → `_load_staging`/`build_run_record` (Task 6); `stat_tag`/`stat_count` (Task 4) → CLI (Task 6) и промпт (Task 8). Словари `FRICTION_TAGS`/`VERIFY_GATE_KINDS`/`STOP_REASONS`/`OUTCOMES`/`COUNTERS` (Task 2) — единый источник.

**Открытые для реализации мелочи (не блокируют, зафиксированы в шагах):**

- Имя итогового файла собрать внутри `stat-build` из `<machine_id>-<plan_id>-<uuid>` (Task 6/7, Step 4 примечание), а не голого uuid из bash.
- `--plan-changed`/`--committed` парсить как 0/1 int → bool (Task 7, Step 3 примечание).
- `--autonomy` при желании прокинуть реальным значением через json-однострочник, как `ORD`/`DEPS_N`.
