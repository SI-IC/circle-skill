# circle-skill Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Плагин Claude Code, автономно исполняющий фазовый план по одной фазе за отдельную интерактивную сессию (под подпиской), в цикле, со стоп-критерием «план не изменился» и подтверждением рискованных фаз.

**Architecture:** Умный препролёт (slash-команда в текущей сессии: найти план → нормализовать формат → классифицировать риск → подтвердить → запустить цикл в фоне) + детерминированный фоновый bash-цикл, который на каждой итерации **сам выбирает следующую фазу** (python-модуль `circle_plan.py`), запускает свежую интерактивную сессию `claude` под PTY (`run_phase.py`), ждёт файл-сигнал `.circle/result`, сравнивает хеш плана и решает, продолжать ли. Выбор фазы и определение завершённости — детерминированный код (тестируемо); саму работу делает Claude-исполнитель и ведёт статусы/журнал.

**Tech Stack:** Bash (POSIX-совместимый, macOS bash 3.2), Python 3 (только stdlib: `pty`, `re`, `argparse`, `hashlib`, `unittest`), Claude Code plugin (plugin.json + slash-команда). Ноль внешних зависимостей (никаких pip/npm-пакетов — ничего не качаем).

**Refinement vs spec:** В спеке исполнитель сигналил `PLAN_COMPLETE`. Здесь завершённость определяет цикл (`circle_plan.py next` → `NONE`), а `result` исполнителя несёт лишь `PHASE_DONE` (сессия закончила работу). Это детерминированнее и корректно ловит циклические `deps`.

---

## File Structure

```
circle-skill/
  .claude-plugin/plugin.json        # манифест плагина
  marketplace.json                  # для установки на любой машине
  commands/circle-skill.md          # /circle-skill <план> — препролёт (LLM)
  scripts/preflight.sh              # поиск плана + проверка окружения (claude, python3)
  scripts/circle_plan.py            # парсинг маркеров, выбор фазы, статусы, сводка (ядро, TDD)
  scripts/run_phase.py              # PTY-раннер: claude под pty, ждёт .circle/result, убивает
  scripts/circle-loop.sh            # детерминированный фоновый цикл
  scripts/executor-prompt.md        # шаблон промпта фазы-исполнителя (@@-плейсхолдеры)
  tests/test_circle_plan.py         # unittest ядра
  tests/test_run_phase.py           # unittest PTY-раннера (fake claude)
  tests/test_loop_integration.py    # e2e: цикл против fake claude (harness, в репо)
  tests/fixtures/fake_claude.py     # поддельный claude для интеграции
  README.md
  .gitignore
```

Каждый файл — одна ответственность. Парсинг/выбор фаз изолирован в `circle_plan.py` (единственный источник логики статусов); PTY-механика — в `run_phase.py`; оркестрация — в `circle-loop.sh`; интеллект/UX — в `commands/circle-skill.md` и `executor-prompt.md`.

---

## Task 0: Scaffold плагина

**Files:**

- Create: `.claude-plugin/plugin.json`
- Create: `marketplace.json`
- Create: `.gitignore`
- Create: `tests/__init__.py` (пустой)

- [ ] **Step 1: plugin.json**

Create `.claude-plugin/plugin.json`:

```json
{
  "name": "circle-skill",
  "description": "Автономное исполнение фазового плана в цикле отдельными интерактивными сессиями (по фазе на сессию), со стоп-критерием отсутствия прогресса и подтверждением рискованных фаз.",
  "version": "0.1.0",
  "author": { "name": "Alex", "email": "shum.sts@gmail.com" }
}
```

- [ ] **Step 2: marketplace.json**

Create `marketplace.json`:

```json
{
  "name": "circle-skill",
  "description": "Маркетплейс плагина circle-skill",
  "owner": { "name": "Alex", "email": "shum.sts@gmail.com" },
  "plugins": [
    {
      "name": "circle-skill",
      "description": "Автономное исполнение фазового плана в цикле отдельными интерактивными сессиями.",
      "version": "0.1.0",
      "source": "./",
      "author": { "name": "Alex", "email": "shum.sts@gmail.com" }
    }
  ]
}
```

- [ ] **Step 3: .gitignore**

Create `.gitignore`:

```
__pycache__/
*.pyc
.circle/
.DS_Store
```

- [ ] **Step 4: пустой tests/**init**.py**

```bash
mkdir -p tests scripts && touch tests/__init__.py
```

- [ ] **Step 5: Проверить, что plugin.json — валидный JSON**

Run: `python3 -c "import json;json.load(open('.claude-plugin/plugin.json'));json.load(open('marketplace.json'));print('JSON OK')"`
Expected: `JSON OK`

- [ ] **Step 6: Commit**

```bash
git add .claude-plugin marketplace.json .gitignore tests/__init__.py
git commit -m "feat: scaffold circle-skill plugin (manifest, marketplace, gitignore)"
```

---

## Task 1: `circle_plan.py` — парсинг фаз и маркеров (TDD)

**Files:**

- Create: `scripts/circle_plan.py`
- Test: `tests/test_circle_plan.py`

- [ ] **Step 1: Написать падающий тест парсинга**

Create `tests/test_circle_plan.py`:

```python
import os, sys, unittest
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))
import circle_plan as cp

PLAN = """# План: тест

## Фаза 1 — Логин
<!-- circle: status=done order=10 deps=[] autonomy=auto obstacle="" -->
Тело фазы 1.

## Фаза 2 — Перенос
<!-- circle: status=pending order=20 deps=[1] autonomy=auto obstacle="" -->
Тело фазы 2.

## Фаза 3 — Дроп прод-таблиц
<!-- circle: status=pending order=30 deps=[2] autonomy=needs-human obstacle="" -->
Тело фазы 3.
"""

class TestParse(unittest.TestCase):
    def test_parses_all_phases(self):
        ph = cp.parse_phases(PLAN)
        self.assertEqual([p.id for p in ph], ["1", "2", "3"])

    def test_parses_marker_fields(self):
        ph = {p.id: p for p in cp.parse_phases(PLAN)}
        self.assertEqual(ph["1"].status, "done")
        self.assertEqual(ph["2"].order, 20)
        self.assertEqual(ph["2"].deps, ["1"])
        self.assertEqual(ph["3"].autonomy, "needs-human")

    def test_title_parsed(self):
        ph = {p.id: p for p in cp.parse_phases(PLAN)}
        self.assertEqual(ph["1"].title, "Логин")

    def test_phase_without_marker_defaults_pending(self):
        text = "## Фаза 9 — Без маркера\nтело\n"
        ph = cp.parse_phases(text)
        self.assertEqual(ph[0].status, "pending")
        self.assertEqual(ph[0].marker_line, -1)

    def test_obstacle_with_spaces(self):
        text = ('## Фаза 5 — X\n'
                '<!-- circle: status=blocked order=1 deps=[] autonomy=auto obstacle="нет доступа к БД" -->\n')
        ph = cp.parse_phases(text)[0]
        self.assertEqual(ph.obstacle, "нет доступа к БД")
        self.assertEqual(ph.status, "blocked")

if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Запустить — убедиться, что падает**

Run: `python3 -m pytest tests/test_circle_plan.py -q 2>/dev/null || python3 -m unittest tests.test_circle_plan -v`
Expected: FAIL/ERROR — `No module named 'circle_plan'` (файла ещё нет).

- [ ] **Step 3: Реализовать парсинг**

Create `scripts/circle_plan.py`:

```python
#!/usr/bin/env python3
"""circle-skill: парсинг фазового плана, выбор фазы, статусы, сводка."""
import re
from dataclasses import dataclass, field

HEADING_RE = re.compile(r'^##\s+Фаза\s+(\S+)\s+[—-]\s+(.+?)\s*$')
MARKER_RE  = re.compile(r'<!--\s*circle:\s*(.*?)\s*-->')

DONE = "done"
VALID_STATUS = {"pending", "in_progress", "done", "blocked", "skipped"}

@dataclass
class Phase:
    id: str
    title: str
    status: str = "pending"
    order: int = 0
    deps: list = field(default_factory=list)
    autonomy: str = "auto"
    obstacle: str = ""
    heading_line: int = -1
    marker_line: int = -1

def _parse_marker(inner: str) -> dict:
    d = {}
    m = re.search(r'status=([\w-]+)', inner);   d['status']   = m.group(1) if m else 'pending'
    m = re.search(r'order=(-?\d+)', inner);      d['order']    = int(m.group(1)) if m else 0
    m = re.search(r'deps=\[([^\]]*)\]', inner)
    d['deps'] = [x.strip() for x in m.group(1).split(',') if x.strip()] if m else []
    m = re.search(r'autonomy=([\w-]+)', inner);  d['autonomy'] = m.group(1) if m else 'auto'
    m = re.search(r'obstacle="((?:[^"\\]|\\.)*)"', inner)
    d['obstacle'] = m.group(1).replace('\\"', '"') if m else ''
    return d

def parse_phases(text: str) -> list:
    lines = text.splitlines()
    phases = []
    for i, line in enumerate(lines):
        hm = HEADING_RE.match(line)
        if not hm:
            continue
        ph = Phase(id=hm.group(1), title=hm.group(2), heading_line=i)
        for j in range(i + 1, min(i + 4, len(lines))):
            if HEADING_RE.match(lines[j]):
                break
            mm = MARKER_RE.search(lines[j])
            if mm:
                v = _parse_marker(mm.group(1))
                ph.status, ph.order = v['status'], v['order']
                ph.deps, ph.autonomy, ph.obstacle = v['deps'], v['autonomy'], v['obstacle']
                ph.marker_line = j
                break
        phases.append(ph)
    return phases
```

- [ ] **Step 4: Запустить тесты — зелёные**

Run: `python3 -m unittest tests.test_circle_plan -v`
Expected: 5 тестов PASS.

- [ ] **Step 5: Commit**

```bash
git add scripts/circle_plan.py tests/test_circle_plan.py
git commit -m "feat: circle_plan parse_phases + marker parsing"
```

---

## Task 2: `circle_plan.py` — выбор фазы и завершённость (TDD)

**Files:**

- Modify: `scripts/circle_plan.py` (добавить `select_next`, `is_complete`)
- Test: `tests/test_circle_plan.py` (добавить класс)

- [ ] **Step 1: Добавить падающий тест**

Append to `tests/test_circle_plan.py` (перед `if __name__`):

```python
class TestSelect(unittest.TestCase):
    def _ph(self, **kw):
        base = dict(id="x", title="t", status="pending", order=0, deps=[], autonomy="auto")
        base.update(kw); return cp.Phase(**base)

    def test_picks_lowest_order_eligible_pending(self):
        phases = [self._ph(id="b", order=20), self._ph(id="a", order=10)]
        self.assertEqual(cp.select_next(phases).id, "a")

    def test_skips_phase_with_unmet_deps(self):
        phases = [self._ph(id="1", status="pending", order=10, deps=["0"])]
        self.assertIsNone(cp.select_next(phases))

    def test_deps_met_when_done(self):
        phases = [self._ph(id="1", status="done", order=10),
                  self._ph(id="2", status="pending", order=20, deps=["1"])]
        self.assertEqual(cp.select_next(phases).id, "2")

    def test_skips_needs_human_and_skipped(self):
        phases = [self._ph(id="1", autonomy="needs-human", order=10),
                  self._ph(id="2", status="skipped", order=20)]
        self.assertIsNone(cp.select_next(phases))

    def test_in_progress_takes_priority(self):
        phases = [self._ph(id="1", status="in_progress", order=99),
                  self._ph(id="2", status="pending", order=1)]
        self.assertEqual(cp.select_next(phases).id, "1")

    def test_is_complete_when_nothing_eligible(self):
        phases = [self._ph(id="1", status="done", order=10),
                  self._ph(id="2", status="blocked", order=20),
                  self._ph(id="3", status="skipped", order=30)]
        self.assertTrue(cp.is_complete(phases))

    def test_not_complete_when_pending_eligible(self):
        phases = [self._ph(id="1", status="pending", order=10)]
        self.assertFalse(cp.is_complete(phases))
```

- [ ] **Step 2: Запустить — упадёт на `select_next`**

Run: `python3 -m unittest tests.test_circle_plan -v`
Expected: ошибки `module 'circle_plan' has no attribute 'select_next'`.

- [ ] **Step 3: Реализовать**

Append to `scripts/circle_plan.py`:

```python
def _dep_done(by_id, dep):
    p = by_id.get(dep)
    return p is not None and p.status == DONE

def select_next(phases):
    by_id = {p.id: p for p in phases}
    inprog = [p for p in phases if p.status == "in_progress"]
    if inprog:
        return sorted(inprog, key=lambda p: (p.order, p.id))[0]
    eligible = [p for p in phases
                if p.status == "pending" and p.autonomy == "auto"
                and all(_dep_done(by_id, d) for d in p.deps)]
    if not eligible:
        return None
    return sorted(eligible, key=lambda p: (p.order, p.id))[0]

def is_complete(phases):
    return select_next(phases) is None
```

- [ ] **Step 4: Тесты зелёные**

Run: `python3 -m unittest tests.test_circle_plan -v`
Expected: все PASS.

- [ ] **Step 5: Commit**

```bash
git add scripts/circle_plan.py tests/test_circle_plan.py
git commit -m "feat: circle_plan select_next + is_complete"
```

---

## Task 3: `circle_plan.py` — запись статусов и маркеров (TDD)

**Files:**

- Modify: `scripts/circle_plan.py` (`_render_marker`, `set_status`, `add_marker`)
- Test: `tests/test_circle_plan.py`

- [ ] **Step 1: Падающий тест**

Append to `tests/test_circle_plan.py`:

```python
class TestWrite(unittest.TestCase):
    def test_set_status_preserves_other_fields(self):
        text = ('## Фаза 2 — X\n'
                '<!-- circle: status=pending order=20 deps=[1] autonomy=auto obstacle="" -->\n'
                'тело\n')
        out = cp.set_status(text, "2", "done")
        p = {x.id: x for x in cp.parse_phases(out)}["2"]
        self.assertEqual(p.status, "done")
        self.assertEqual(p.order, 20)
        self.assertEqual(p.deps, ["1"])

    def test_set_status_writes_obstacle(self):
        text = ('## Фаза 2 — X\n'
                '<!-- circle: status=pending order=20 deps=[] autonomy=auto obstacle="" -->\n')
        out = cp.set_status(text, "2", "blocked", obstacle='нет доступа "root"')
        p = cp.parse_phases(out)[0]
        self.assertEqual(p.status, "blocked")
        self.assertEqual(p.obstacle, 'нет доступа "root"')

    def test_set_status_missing_marker_raises(self):
        text = "## Фаза 2 — X\nтело\n"
        with self.assertRaises(ValueError):
            cp.set_status(text, "2", "done")

    def test_add_marker_inserts_under_heading(self):
        text = "## Фаза 7 — Новая\nтело\n"
        out = cp.add_marker(text, "7", status="pending", order=70, deps=["1"], autonomy="needs-human")
        p = cp.parse_phases(out)[0]
        self.assertEqual(p.marker_line, 1)
        self.assertEqual(p.order, 70)
        self.assertEqual(p.autonomy, "needs-human")

    def test_add_marker_idempotent_overwrites(self):
        text = ('## Фаза 7 — Новая\n'
                '<!-- circle: status=pending order=1 deps=[] autonomy=auto obstacle="" -->\n')
        out = cp.add_marker(text, "7", status="done", order=70)
        ph = cp.parse_phases(out)
        self.assertEqual(len(ph), 1)
        self.assertEqual(ph[0].status, "done")
```

- [ ] **Step 2: Запустить — упадёт**

Run: `python3 -m unittest tests.test_circle_plan -v`
Expected: ошибки на `set_status`/`add_marker`.

- [ ] **Step 3: Реализовать**

Append to `scripts/circle_plan.py`:

```python
def _render_marker(status, order, deps, autonomy, obstacle):
    ob = obstacle.replace('"', '\\"')
    return (f'<!-- circle: status={status} order={order} '
            f'deps=[{",".join(deps)}] autonomy={autonomy} obstacle="{ob}" -->')

def _find(phases, phase_id):
    for p in phases:
        if p.id == phase_id:
            return p
    raise KeyError(f"фаза {phase_id} не найдена")

def _reassemble(raw, original_text):
    out = "\n".join(raw)
    if original_text.endswith("\n"):
        out += "\n"
    return out

def set_status(text, phase_id, status, obstacle=None):
    if status not in VALID_STATUS:
        raise ValueError(f"недопустимый статус: {status}")
    raw = text.splitlines()
    t = _find(parse_phases(text), phase_id)
    if t.marker_line < 0:
        raise ValueError(f"у фазы {phase_id} нет circle-маркера (сначала add-marker)")
    ob = t.obstacle if obstacle is None else obstacle
    raw[t.marker_line] = _render_marker(status, t.order, t.deps, t.autonomy, ob)
    return _reassemble(raw, text)

def add_marker(text, phase_id, status="pending", order=0, deps=None, autonomy="auto", obstacle=""):
    deps = deps or []
    raw = text.splitlines()
    t = _find(parse_phases(text), phase_id)
    marker = _render_marker(status, order, deps, autonomy, obstacle)
    if t.marker_line >= 0:
        raw[t.marker_line] = marker
    else:
        raw.insert(t.heading_line + 1, marker)
    return _reassemble(raw, text)
```

- [ ] **Step 4: Тесты зелёные**

Run: `python3 -m unittest tests.test_circle_plan -v`
Expected: все PASS.

- [ ] **Step 5: Commit**

```bash
git add scripts/circle_plan.py tests/test_circle_plan.py
git commit -m "feat: circle_plan set_status + add_marker"
```

---

## Task 4: `circle_plan.py` — сводка + CLI (TDD)

**Files:**

- Modify: `scripts/circle_plan.py` (`summary`, `main`/argparse)
- Test: `tests/test_circle_plan.py`

- [ ] **Step 1: Падающий тест сводки + CLI**

Append to `tests/test_circle_plan.py`:

```python
import json, subprocess, tempfile

class TestSummaryCLI(unittest.TestCase):
    PLAN = ('## Фаза 1 — A\n<!-- circle: status=done order=10 deps=[] autonomy=auto obstacle="" -->\n'
            '## Фаза 2 — B\n<!-- circle: status=blocked order=20 deps=[] autonomy=auto obstacle="нет БД" -->\n'
            '## Фаза 3 — C\n<!-- circle: status=pending order=30 deps=[1] autonomy=auto obstacle="" -->\n')

    def test_summary_counts(self):
        s = cp.summary(cp.parse_phases(self.PLAN))
        self.assertEqual(s["counts"]["done"], 1)
        self.assertEqual(s["counts"]["blocked"], 1)
        self.assertEqual(s["blocked"][0]["obstacle"], "нет БД")

    def _write(self, text):
        f = tempfile.NamedTemporaryFile("w", suffix=".md", delete=False, encoding="utf-8")
        f.write(text); f.close(); return f.name

    def _cli(self, *args):
        script = os.path.join(os.path.dirname(__file__), "..", "scripts", "circle_plan.py")
        return subprocess.run([sys.executable, script, *args], capture_output=True, text=True)

    def test_cli_next_prints_id_tab_title(self):
        path = self._write(self.PLAN)
        r = self._cli("next", path)
        self.assertEqual(r.returncode, 0)
        self.assertEqual(r.stdout.strip(), "3\tC")

    def test_cli_next_none_when_complete(self):
        path = self._write(self.PLAN.replace('status=pending order=30 deps=[1]',
                                             'status=done order=30 deps=[1]'))
        r = self._cli("next", path)
        self.assertEqual(r.stdout.strip(), "NONE")

    def test_cli_set_status_roundtrip(self):
        path = self._write(self.PLAN)
        self._cli("set-status", path, "3", "done")
        with open(path, encoding="utf-8") as fh:
            p = {x.id: x for x in cp.parse_phases(fh.read())}
        self.assertEqual(p["3"].status, "done")

    def test_cli_phases_json(self):
        path = self._write(self.PLAN)
        r = self._cli("phases", path, "--json")
        data = json.loads(r.stdout)
        self.assertEqual(len(data), 3)
        self.assertEqual(data[0]["id"], "1")
```

- [ ] **Step 2: Запустить — упадёт**

Run: `python3 -m unittest tests.test_circle_plan -v`
Expected: ошибки на `summary` и на CLI (нет `main`).

- [ ] **Step 3: Реализовать summary + CLI**

Append to `scripts/circle_plan.py`:

```python
from collections import Counter

def summary(phases):
    counts = Counter(p.status for p in phases)
    def lst(s):
        return [{"id": p.id, "title": p.title, "obstacle": p.obstacle}
                for p in phases if p.status == s]
    return {
        "total": len(phases),
        "counts": dict(counts),
        "done": lst("done"),
        "pending": lst("pending"),
        "in_progress": lst("in_progress"),
        "blocked": lst("blocked"),
        "skipped": lst("skipped"),
        "needs_human": [{"id": p.id, "title": p.title}
                        for p in phases if p.autonomy == "needs-human" and p.status != "done"],
        "complete": is_complete(phases),
    }

def _phase_dict(p):
    return {"id": p.id, "title": p.title, "status": p.status, "order": p.order,
            "deps": p.deps, "autonomy": p.autonomy, "obstacle": p.obstacle}

def _read(path):
    with open(path, encoding="utf-8") as f:
        return f.read()

def _write_file(path, text):
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)

def main(argv=None):
    import argparse, json, sys
    ap = argparse.ArgumentParser(prog="circle_plan")
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name in ("next", "complete", "summary", "phases"):
        sp = sub.add_parser(name); sp.add_argument("plan")
        if name in ("summary", "phases"):
            sp.add_argument("--json", action="store_true")
    sp = sub.add_parser("set-status"); sp.add_argument("plan"); sp.add_argument("id")
    sp.add_argument("status"); sp.add_argument("--obstacle", default=None)
    sp = sub.add_parser("add-marker"); sp.add_argument("plan"); sp.add_argument("id")
    sp.add_argument("--status", default="pending"); sp.add_argument("--order", type=int, default=0)
    sp.add_argument("--deps", default=""); sp.add_argument("--autonomy", default="auto")
    sp.add_argument("--obstacle", default="")
    a = ap.parse_args(argv)

    phases = parse_phases(_read(a.plan))
    if a.cmd == "next":
        nxt = select_next(phases)
        print("NONE" if nxt is None else f"{nxt.id}\t{nxt.title}")
        return 0
    if a.cmd == "complete":
        return 0 if is_complete(phases) else 1
    if a.cmd == "summary":
        s = summary(phases)
        if getattr(a, "json", False):
            print(json.dumps(s, ensure_ascii=False, indent=2))
        else:
            print(f"Всего фаз: {s['total']}  |  " +
                  "  ".join(f"{k}={v}" for k, v in sorted(s["counts"].items())))
            for b in s["blocked"]:
                print(f"  blocked  {b['id']} — {b['title']}: {b['obstacle']}")
            for b in s["skipped"]:
                print(f"  skipped  {b['id']} — {b['title']}")
        return 0
    if a.cmd == "phases":
        data = [_phase_dict(p) for p in phases]
        print(json.dumps(data, ensure_ascii=False, indent=2) if a.json
              else "\n".join(f"{p['id']}\t{p['status']}\t{p['autonomy']}\t{p['title']}" for p in data))
        return 0
    if a.cmd == "set-status":
        _write_file(a.plan, set_status(_read(a.plan), a.id, a.status, obstacle=a.obstacle))
        return 0
    if a.cmd == "add-marker":
        deps = [x.strip() for x in a.deps.split(",") if x.strip()]
        _write_file(a.plan, add_marker(_read(a.plan), a.id, status=a.status, order=a.order,
                                       deps=deps, autonomy=a.autonomy, obstacle=a.obstacle))
        return 0
    return 2

if __name__ == "__main__":
    import sys
    sys.exit(main())
```

- [ ] **Step 4: Тесты зелёные**

Run: `python3 -m unittest tests.test_circle_plan -v`
Expected: все PASS.

- [ ] **Step 5: Commit**

```bash
git add scripts/circle_plan.py tests/test_circle_plan.py
git commit -m "feat: circle_plan summary + CLI (next/complete/summary/phases/set-status/add-marker)"
```

---

## Task 5: `run_phase.py` — PTY-раннер (TDD)

**Files:**

- Create: `scripts/run_phase.py`
- Test: `tests/test_run_phase.py`

- [ ] **Step 1: Падающий тест (fake-команды, без реального claude)**

Create `tests/test_run_phase.py`:

```python
import os, sys, subprocess, tempfile, time, unittest

SCRIPT = os.path.join(os.path.dirname(__file__), "..", "scripts", "run_phase.py")

def run(result, timeout, cmd):
    return subprocess.run(
        [sys.executable, SCRIPT, "--result", result, "--timeout", str(timeout), "--", *cmd],
        capture_output=True, text=True, timeout=60)

class TestRunPhase(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp()
        self.result = os.path.join(self.d, "result")

    def test_returns_0_when_result_appears(self):
        cmd = [sys.executable, "-c",
               f"import time;time.sleep(0.3);open({self.result!r},'w').write('CIRCLE_RESULT: PHASE_DONE')"]
        r = run(self.result, 10, cmd)
        self.assertEqual(r.returncode, 0)
        self.assertTrue(os.path.exists(self.result))

    def test_returns_2_on_timeout_and_kills(self):
        cmd = [sys.executable, "-c", "import time;time.sleep(30)"]
        t0 = time.monotonic()
        r = run(self.result, 1, cmd)
        self.assertEqual(r.returncode, 2)
        self.assertLess(time.monotonic() - t0, 15)  # реально прервал, не ждал 30с

    def test_returns_3_when_child_exits_without_result(self):
        cmd = [sys.executable, "-c", "import sys;sys.exit(0)"]
        r = run(self.result, 10, cmd)
        self.assertEqual(r.returncode, 3)

if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Запустить — упадёт (нет файла)**

Run: `python3 -m unittest tests.test_run_phase -v`
Expected: ошибки — `run_phase.py` не существует.

- [ ] **Step 3: Реализовать PTY-раннер**

Create `scripts/run_phase.py`:

```python
#!/usr/bin/env python3
"""Запускает команду (интерактивный claude) под PTY; ждёт появления файла-результата,
затем убивает процесс. Коды: 0=result появился, 2=таймаут, 3=процесс вышел без result."""
import argparse, os, select, signal, sys, time

def _terminate(pid):
    for sig in (signal.SIGTERM, signal.SIGKILL):
        try:
            os.kill(pid, sig)
        except ProcessLookupError:
            return
        for _ in range(20):
            try:
                wpid, _ = os.waitpid(pid, os.WNOHANG)
            except ChildProcessError:
                return
            if wpid == pid:
                return
            time.sleep(0.1)

def _result_ready(path):
    try:
        return os.path.getsize(path) > 0
    except OSError:
        return False

def main(argv=None):
    import pty
    ap = argparse.ArgumentParser()
    ap.add_argument("--result", required=True)
    ap.add_argument("--timeout", type=float, default=3600.0)
    ap.add_argument("--log", default=None)
    ap.add_argument("--poll", type=float, default=1.0)
    ap.add_argument("cmd", nargs=argparse.REMAINDER)
    a = ap.parse_args(argv)
    cmd = a.cmd[1:] if a.cmd and a.cmd[0] == "--" else a.cmd
    if not cmd:
        print("run_phase: пустая команда", file=sys.stderr)
        return 4

    try:
        os.unlink(a.result)
    except FileNotFoundError:
        pass

    pid, fd = pty.fork()
    if pid == 0:  # child
        try:
            os.execvp(cmd[0], cmd)
        except Exception as e:
            sys.stderr.write(f"exec failed: {e}\n")
        os._exit(127)

    logf = open(a.log, "ab") if a.log else None
    start = time.monotonic()
    rc = 0
    child_alive = True
    try:
        while True:
            try:
                r, _, _ = select.select([fd], [], [], a.poll)
            except (OSError, ValueError):
                r = []
            if r:
                try:
                    data = os.read(fd, 65536)
                except OSError:
                    data = b""
                if data and logf:
                    logf.write(data); logf.flush()
            if _result_ready(a.result):
                rc = 0; break
            try:
                wpid, _ = os.waitpid(pid, os.WNOHANG)
            except ChildProcessError:
                wpid = pid
            if wpid == pid:
                child_alive = False
                rc = 0 if _result_ready(a.result) else 3
                break
            if time.monotonic() - start > a.timeout:
                rc = 2; break
    finally:
        if child_alive:
            _terminate(pid)
        if logf:
            logf.close()
        try:
            os.close(fd)
        except OSError:
            pass
    return rc

if __name__ == "__main__":
    sys.exit(main())
```

- [ ] **Step 4: Тесты зелёные**

Run: `python3 -m unittest tests.test_run_phase -v`
Expected: 3 теста PASS.

- [ ] **Step 5: Commit**

```bash
git add scripts/run_phase.py tests/test_run_phase.py
git commit -m "feat: run_phase PTY runner (result-file signaled, timeout-killed)"
```

---

## Task 6: `executor-prompt.md` — шаблон промпта исполнителя

**Files:**

- Create: `scripts/executor-prompt.md`

- [ ] **Step 1: Создать шаблон**

Create `scripts/executor-prompt.md`:

```markdown
# Инструкция фазе-исполнителю circle-skill

Ты — автономная сессия Claude Code, выполняющая РОВНО ОДНУ фазу согласованного плана.
Сессия **неинтерактивна**: не задавай вопросов пользователю, не жди подтверждений — действуй сам.

- План: `@@PLAN@@`
- Назначенная фаза: **@@PHASE_ID@@**
- Рабочая папка плагина: `@@WORK@@`
- CLI статусов: `python3 @@PLAN_CLI@@`

## Порядок действий

1. Открой план `@@PLAN@@`. Прочитай раздел «## Фаза @@PHASE_ID@@», а также секции контекста,
   стратегии и рисков плана. Если статус фазы `in_progress` — это недоделанный остаток прошлой
   сессии: разберись, что уже сделано, и доводи до конца либо откати.
2. Пометь фазу в работе:
   `python3 @@PLAN_CLI@@ set-status @@PLAN@@ @@PHASE_ID@@ in_progress`
3. Если видно, что фаза слишком велика для одной сессии — раздроби её на подфазы: добавь новые
   заголовки `## Фаза @@PHASE_ID@@a — …`, `## Фаза @@PHASE_ID@@b — …` (через Edit) и маркеры:
   `python3 @@PLAN_CLI@@ add-marker @@PLAN@@ @@PHASE_ID@@a --status pending --order <n> --deps <...> --autonomy auto`
   Текущую фазу тогда пометь `done` (её работа теперь в подфазах) и переходи к шагу 5.
4. Выполни фазу полностью. Доступны все инструменты (Bash/SSH/БД и т. д.) и субагенты.
   Соблюдай verify-требования из текста фазы.
   - **Успех** (verify зелёный):
     `python3 @@PLAN_CLI@@ set-status @@PLAN@@ @@PHASE_ID@@ done`
   - **Неуспех/препятствие**: ОТКАТИ свои изменения. git → `git reset --hard`/`git checkout`/`git stash`.
     Не-git (удалённый сервер, БД) → обратными операциями, best-effort. Необратимое → честно
     опиши в obstacle, что осталось изменённым. Затем:
     `python3 @@PLAN_CLI@@ set-status @@PLAN@@ @@PHASE_ID@@ blocked --obstacle "кратко: что помешало"`
5. **Обязательный шаг — всегда:** допиши в секцию «## Журнал» плана запись 5–8 строк
   (что сделал, verify, был ли откат, следующий шаг). Если секции «## Журнал» нет — создай её в конце файла.
6. **Последнее действие** (сигнал циклу, что сессия закончила работу):
   `printf 'CIRCLE_RESULT: PHASE_DONE\n' > @@WORK@@/result`

Никогда не трогай фазы со статусом `skipped` или `autonomy=needs-human` — они исключены владельцем.
```

- [ ] **Step 2: Проверить, что все плейсхолдеры присутствуют**

Run: `for ph in @@PLAN@@ @@PHASE_ID@@ @@WORK@@ @@PLAN_CLI@@; do grep -q -- "$ph" scripts/executor-prompt.md && echo "$ph OK" || echo "$ph MISSING"; done`
Expected: все четыре `OK`.

- [ ] **Step 3: Commit**

```bash
git add scripts/executor-prompt.md
git commit -m "feat: executor prompt template for phase sessions"
```

---

## Task 7: `circle-loop.sh` — детерминированный фоновый цикл

**Files:**

- Create: `scripts/circle-loop.sh`

- [ ] **Step 1: Создать скрипт**

Create `scripts/circle-loop.sh`:

```bash
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
```

- [ ] **Step 2: Сделать исполняемым + shellcheck-проверка синтаксиса**

Run: `chmod +x scripts/circle-loop.sh && bash -n scripts/circle-loop.sh && echo "syntax OK"`
Expected: `syntax OK`

- [ ] **Step 3: Commit**

```bash
git add scripts/circle-loop.sh
git commit -m "feat: circle-loop.sh deterministic background orchestration loop"
```

---

## Task 8: Интеграционный тест цикла против fake claude (e2e harness)

**Files:**

- Create: `tests/fixtures/fake_claude.py`
- Create: `tests/test_loop_integration.py`

- [ ] **Step 1: Создать поддельный claude**

Этот фейк имитирует сессию: находит в своих argv путь к `executor-prompt.md`, читает оттуда
реальный путь плана и id фазы, и выполняет «работу» по сценарию из переменной окружения
`FAKE_MODE` (`done` — пометить фазу done + журнал; `nothing` — ничего; `hang` — зависнуть).

Create `tests/fixtures/fake_claude.py`:

```python
#!/usr/bin/env python3
"""Поддельный claude для интеграционного теста цикла. НЕ ходит в сеть."""
import os, re, sys, time

def find_prompt_path(argv):
    for a in argv:
        m = re.search(r'(\S+executor-prompt\.md)', a)
        if m:
            return m.group(1)
    return None

def main():
    mode = os.environ.get("FAKE_MODE", "done")
    if mode == "hang":
        time.sleep(300)
        return 0
    prompt_path = find_prompt_path(sys.argv)
    if not prompt_path or not os.path.exists(prompt_path):
        return 1
    text = open(prompt_path, encoding="utf-8").read()
    plan = re.search(r'План:\s*`([^`]+)`', text).group(1)
    phase = re.search(r'Назначенная фаза:\s*\*\*([^*]+)\*\*', text).group(1)
    work = re.search(r'Рабочая папка плагина:\s*`([^`]+)`', text).group(1)
    plan_cli = re.search(r'CLI статусов:\s*`python3 ([^`]+)`', text).group(1)

    if mode == "done":
        os.system(f'{sys.executable} {plan_cli} set-status {plan} {phase} done')
        with open(plan, "a", encoding="utf-8") as f:
            f.write(f"\n### fake: фаза {phase} выполнена\n")
    # mode == "nothing": план не трогаем (эмуляция зависшей-без-прогресса сессии)

    # сигнал циклу
    with open(os.path.join(work, "result"), "w", encoding="utf-8") as f:
        f.write("CIRCLE_RESULT: PHASE_DONE\n")
    return 0

if __name__ == "__main__":
    sys.exit(main())
```

- [ ] **Step 2: Написать интеграционный тест**

Create `tests/test_loop_integration.py`:

```python
import os, subprocess, sys, tempfile, unittest

ROOT = os.path.join(os.path.dirname(__file__), "..")
LOOP = os.path.join(ROOT, "scripts", "circle-loop.sh")
FAKE = os.path.join(ROOT, "tests", "fixtures", "fake_claude.py")

PLAN = (
    "# План тест\n\n"
    "## Фаза 1 — A\n<!-- circle: status=pending order=10 deps=[] autonomy=auto obstacle=\"\" -->\n\n"
    "## Фаза 2 — B\n<!-- circle: status=pending order=20 deps=[1] autonomy=auto obstacle=\"\" -->\n\n"
    "## Фаза 3 — C\n<!-- circle: status=pending order=30 deps=[] autonomy=needs-human obstacle=\"\" -->\n\n"
    "## Журнал\n"
)

def run_loop(plan_path, fake_mode, timeout="5"):
    env = dict(os.environ)
    env["CIRCLE_CLAUDE_BIN"] = f"{sys.executable}"   # см. ниже: оборачиваем фейк
    env["FAKE_MODE"] = fake_mode
    env["CIRCLE_TIMEOUT"] = timeout
    env["CLAUDE_PLUGIN_ROOT"] = ROOT
    # claude-bin = python3, поэтому первый arg цикла к claude — флаг; вместо этого
    # подменяем через обёртку-скрипт:
    wrapper = plan_path + ".claude.sh"
    with open(wrapper, "w") as f:
        f.write(f'#!/usr/bin/env bash\nexec "{sys.executable}" "{FAKE}" "$@"\n')
    os.chmod(wrapper, 0o755)
    env["CIRCLE_CLAUDE_BIN"] = wrapper
    return subprocess.run(["bash", LOOP, plan_path], env=env,
                          capture_output=True, text=True, timeout=120)

class TestLoopIntegration(unittest.TestCase):
    def _plan(self):
        d = tempfile.mkdtemp()
        p = os.path.join(d, "plan.md")
        open(p, "w", encoding="utf-8").write(PLAN)
        return p

    def _summary(self, plan):
        return open(os.path.join(os.path.dirname(plan), ".circle", "summary.txt"),
                    encoding="utf-8").read()

    def test_runs_to_completion_skipping_needs_human(self):
        plan = self._plan()
        run_loop(plan, "done")
        s = self._summary(plan)
        self.assertIn("STOP_REASON=complete", s)
        text = open(plan, encoding="utf-8").read()
        import sys as _s; _s.path.insert(0, os.path.join(ROOT, "scripts"))
        import circle_plan as cp
        ph = {p.id: p for p in cp.parse_phases(text)}
        self.assertEqual(ph["1"].status, "done")
        self.assertEqual(ph["2"].status, "done")
        self.assertEqual(ph["3"].status, "pending")   # needs-human не тронута

    def test_stops_on_no_progress(self):
        plan = self._plan()
        run_loop(plan, "nothing")
        s = self._summary(plan)
        self.assertIn("STOP_REASON=no-progress", s)

    def test_stops_on_hang_timeout(self):
        plan = self._plan()
        run_loop(plan, "hang", timeout="2")
        s = self._summary(plan)
        self.assertIn("STOP_REASON=hang", s)

if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 3: Запустить интеграционные тесты**

Run: `python3 -m unittest tests.test_loop_integration -v`
Expected: 3 теста PASS (complete / no-progress / hang).

- [ ] **Step 4: Прогнать ВСЕ тесты**

Run: `python3 -m unittest discover -s tests -v`
Expected: все тесты (circle_plan + run_phase + loop) PASS.

- [ ] **Step 5: Commit**

```bash
git add tests/fixtures/fake_claude.py tests/test_loop_integration.py
git commit -m "test: e2e loop integration harness with fake claude (complete/no-progress/hang)"
```

---

## Task 9: `preflight.sh` — поиск плана + проверка окружения

**Files:**

- Create: `scripts/preflight.sh`
- Test: `tests/test_preflight.py`

- [ ] **Step 1: Падающий тест**

Create `tests/test_preflight.py`:

```python
import os, subprocess, tempfile, unittest

SH = os.path.join(os.path.dirname(__file__), "..", "scripts", "preflight.sh")

def run(arg, cwd):
    return subprocess.run(["bash", SH, arg], cwd=cwd, capture_output=True, text=True)

class TestPreflight(unittest.TestCase):
    def test_resolves_explicit_path(self):
        d = tempfile.mkdtemp()
        p = os.path.join(d, "myplan.md"); open(p, "w").write("# x")
        r = run(p, d)
        self.assertIn("CIRCLE_PREFLIGHT: OK", r.stdout)
        self.assertIn(f"PLAN={p}", r.stdout)

    def test_resolves_by_name_glob(self):
        d = tempfile.mkdtemp()
        open(os.path.join(d, "2026-06-03-superplan.md"), "w").write("# x")
        r = run("superplan", d)
        self.assertIn("CIRCLE_PREFLIGHT: OK", r.stdout)

    def test_error_when_not_found(self):
        d = tempfile.mkdtemp()
        r = run("nonexistent", d)
        self.assertIn("CIRCLE_PREFLIGHT: ERROR", r.stdout)
```

- [ ] **Step 2: Запустить — упадёт (нет файла)**

Run: `python3 -m unittest tests.test_preflight -v`
Expected: ошибки — `preflight.sh` не существует.

- [ ] **Step 3: Реализовать**

Create `scripts/preflight.sh`:

```bash
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
echo "WORK=$(dirname "$PLAN")/.circle"
echo "claude=$(command -v claude)  python3=$(command -v python3)"
```

- [ ] **Step 4: Тесты зелёные**

Run: `chmod +x scripts/preflight.sh && python3 -m unittest tests.test_preflight -v`
Expected: 3 теста PASS.

- [ ] **Step 5: Commit**

```bash
git add scripts/preflight.sh tests/test_preflight.py
git commit -m "feat: preflight.sh plan resolution + env checks"
```

---

## Task 10: `commands/circle-skill.md` — препролёт-команда (LLM)

**Files:**

- Create: `commands/circle-skill.md`

- [ ] **Step 1: Создать команду**

Create `commands/circle-skill.md`:

```markdown
---
description: Автономно исполнить фазовый план в цикле отдельными фоновыми сессиями (по фазе на сессию)
argument-hint: <путь-или-имя-плана>
allowed-tools: Bash, Read, Edit, AskUserQuestion, BashOutput
---

## Препроверка окружения и поиск плана

!`bash "${CLAUDE_PLUGIN_ROOT}/scripts/preflight.sh" "$ARGUMENTS"`

## Твоя задача — запустить плагин circle-skill

Действуй строго по шагам. Язык общения с пользователем — русский.

1. **Проверь вывод preflight выше.**
   - `CIRCLE_PREFLIGHT: ERROR …` или `AMBIGUOUS …` → останови, покажи проблему пользователю,
     попроси указать точный путь к плану. НЕ продолжай.
   - `CIRCLE_PREFLIGHT: OK` → запомни `PLAN=…` и `WORK=…` из вывода.

2. **Прочитай план** (`Read` по пути PLAN). Найди все заголовки `## Фаза …`.

3. **Нормализация формата.** Проверь, есть ли у фаз circle-маркеры (`<!-- circle: ... -->`).
   - Если у всех фаз маркеры уже есть — пропусти нормализацию.
   - Если маркеров нет (или не у всех): определи для каждой фазы:
     - `status` — **выведи из секции «## Журнал»**: фазы, помеченные в журнале как
       выполненные/задеплоенные/закрытые → `done`; остальные → `pending`.
     - `order` — по порядку появления в файле (10, 20, 30, …).
     - `deps` — из явных зависимостей в тексте (например «после Фазы 2») — список id; иначе `[]`.
     - `autonomy` — `needs-human` если фаза несёт необратимый прод-риск или требует человека
       (сигналы: `DROP`, деплой/deploy, «прод», SSH, «бэкап», «владелец», «СТОП-точка»,
       «необратимо», ручной прогон на проде); иначе `auto`.
       Затем для каждой фазы примени маркер детерминированно:
       `Bash: python3 "${CLAUDE_PLUGIN_ROOT}/scripts/circle_plan.py" add-marker <PLAN> <id> --status <s> --order <n> --deps <c,s,v> --autonomy <auto|needs-human>`
   - Покажи пользователю получившуюся разметку:
     `Bash: python3 "${CLAUDE_PLUGIN_ROOT}/scripts/circle_plan.py" summary <PLAN>`

4. **Классификация риска.** Собери список фаз с `autonomy=needs-human` (и `status` ещё не `done`).

5. **Подтверждение (человек в цикле).**
   - Если таких фаз нет — пропусти шаг.
   - Иначе через `AskUserQuestion` (multiSelect=true) покажи список рискованных фаз и спроси:
     «Какие из этих фаз разрешаешь выполнять АВТОНОМНО, без твоего участия?» Каждая фаза — опция.
   - Для **выбранных** → разрешено: `circle_plan.py set-status <PLAN> <id> pending`
     и переставь `autonomy` на `auto`: `circle_plan.py add-marker <PLAN> <id> --status pending --order <n> --deps <...> --autonomy auto` (сохрани прежние order/deps).
   - Для **невыбранных** → `circle_plan.py set-status <PLAN> <id> skipped --obstacle "не подтверждена владельцем для автономного исполнения"`.

6. **Запусти цикл в фоне.**
   `Bash (run_in_background=true): bash "${CLAUDE_PLUGIN_ROOT}/scripts/circle-loop.sh" "<PLAN>"`
   Сообщи пользователю: цикл запущен в фоне, исполняет фазы по одной свежей сессией каждая;
   прогресс — в `<WORK>/loop.log`; по завершении ты дашь финальный отчёт.

7. **Финальный отчёт.** Когда фоновый процесс завершится (харнесс разбудит тебя уведомлением о
   завершении bg-задачи), прочитай `<WORK>/summary.txt` и выдай отчёт:
   - Причина остановки (`complete` — план исполнен; `no-progress` — план не изменился;
     `hang` — таймаут сессии; `crash`/`error` — сбой).
   - Перечень невыполненных фаз: `blocked` (с obstacle), `skipped`, `needs-human` — с причинами.
```

- [ ] **Step 2: Проверить фронтматтер — валидный YAML + нужные поля**

Run: `python3 -c "import sys; t=open('commands/circle-skill.md').read(); assert t.startswith('---'); fm=t.split('---')[1]; assert 'description:' in fm and 'argument-hint:' in fm; print('frontmatter OK')"`
Expected: `frontmatter OK`

- [ ] **Step 3: Commit**

```bash
git add commands/circle-skill.md
git commit -m "feat: /circle-skill preflight command (normalize, classify, confirm, launch)"
```

---

## Task 11: README + финальная проверка

**Files:**

- Create: `README.md`

- [ ] **Step 1: README**

Create `README.md`:

````markdown
# circle-skill

Плагин Claude Code для автономного исполнения **фазового плана** в цикле: по одной фазе за
отдельную интерактивную сессию, до полного выполнения плана либо до остановки по отсутствию
прогресса. Каждая фаза идёт в свежей сессии — контекстное окно остаётся компактным.

## Как работает

1. `/circle-skill <путь-или-имя-плана>` — препролёт: находит план, нормализует формат
   (проставляет circle-маркеры, статусы выводит из «## Журнал»), классифицирует фазы по риску
   и спрашивает, какие рискованные/прод-фазы разрешить выполнять без тебя.
2. Запускает фоновый цикл. На каждой итерации цикл сам выбирает следующую подходящую фазу и
   запускает свежую интерактивную сессию `claude` под PTY, которая выполняет ровно одну фазу,
   ведёт статус и дописывает журнал.
3. Останавливается, когда подходящих фаз не осталось (план исполнен) **или** план не изменился
   после сессии (нет прогресса) **или** сессия зависла (таймаут). Затем — финальный отчёт.

## Почему интерактивные сессии, а не `claude -p`

`claude -p` (headless) тарифицируется как программное использование (API). Плагин гоняет
**настоящие интерактивные** сессии под PTY — они идут по подписке, видят установленные плагины
и умеют спавнить субагентов.

## Требования

- `claude` в PATH (логин по подписке; `ANTHROPIC_API_KEY` цикл намеренно сбрасывает).
- `python3` (только стандартная библиотека). macOS/Linux; Windows — через WSL.

## Установка

```bash
claude plugin marketplace add <git-url-этого-репозитория>
claude plugin install circle-skill@circle-skill
```
````

## Формат плана

Markdown. Фазы — секции `## Фаза <id> — <title>` с маркером под заголовком:

```
## Фаза 2 — Перенос
<!-- circle: status=pending order=20 deps=[1] autonomy=auto obstacle="" -->
```

Поля: `status` (`pending|in_progress|done|blocked|skipped`), `order`, `deps` (id предшественников,
должны быть `done`), `autonomy` (`auto|needs-human`), `obstacle`. Плюс append-only секция `## Журнал`.

## Настройки (env)

- `CIRCLE_TIMEOUT` — таймаут сессии (сек, по умолч. 3600) — детектор зависания.
- `CIRCLE_PYTHON` — python-интерпретатор (по умолч. `python3`).
- `CIRCLE_CLAUDE_BIN` — бинарь claude (по умолч. `claude`).

## Тесты

```bash
python3 -m unittest discover -s tests -v
```

## Ручной smoke (реальная сессия, под подпиской)

Юнит/интеграционные тесты используют поддельный claude. Для проверки на реальной сессии:
создай минимальный план с одной `auto`-фазой (например «создай файл hello.txt с текстом hi»),
запусти `/circle-skill <plan>`, подтверди (рискованных фаз нет) и убедись, что фаза выполнена,
статус `done`, журнал дописан, цикл остановился с `complete`.

````

- [ ] **Step 2: Прогнать весь тест-сьют**

Run: `python3 -m unittest discover -s tests -v`
Expected: все тесты PASS (circle_plan, run_phase, loop integration, preflight).

- [ ] **Step 3: Проверить синтаксис всех bash-скриптов**

Run: `for s in scripts/*.sh; do bash -n "$s" && echo "$s OK"; done`
Expected: все `OK`.

- [ ] **Step 4: Commit**

```bash
git add README.md
git commit -m "docs: README (usage, install, plan format, manual smoke)"
````

---

## Task 12: Реальный smoke на живой сессии (ручной, под подпиской)

> Не в CI: использует реальный `claude` (подписка). Выполняется владельцем/инженером один раз.

- [ ] **Step 1: Минимальный план**

Create `/tmp/circle-smoke/plan.md`:

```markdown
# Smoke-план

## Фаза 1 — Привет

Создай файл `/tmp/circle-smoke/hello.txt` с текстом `hi from circle-skill`. Verify: файл существует и содержит строку.

## Журнал
```

- [ ] **Step 2: Запустить плагин**

В Claude Code (с установленным плагином), cwd `/tmp/circle-smoke`:
`/circle-skill plan.md`
Подтвердить препролёт (рискованных фаз нет).

- [ ] **Step 3: Проверить результат**

Run: `cat /tmp/circle-smoke/hello.txt; echo "---"; cat /tmp/circle-smoke/.circle/summary.txt`
Expected: `hi from circle-skill`; `STOP_REASON=complete`; в плане фаза 1 = `done`, журнал дописан.

- [ ] **Step 4: Подтвердить биллинг**

Убедиться, что сессия прошла по подписке (в `claude` usage / без списания API-кредитов).
Это финальная проверка ключевого ограничения.

---

## Self-Review (заполняется при написании плана)

**Spec coverage:**

- Формат плана (маркеры/журнал) → Tasks 1–4, executor-prompt (6), README (11). ✔
- Нормализация из Журнала → команда (10) шаг 3 + `add-marker` (3). ✔
- Препролёт безопасности + подтверждение → команда (10) шаги 4–5, `AskUserQuestion`. ✔
- PTY-сессии под подпиской (не `-p`, плагины, субагенты) → run_phase (5), loop unset API key (7), README. ✔
- Выбор фазы / завершённость → `select_next`/`is_complete` (2). ✔
- Стоп по неизменности плана + таймаут зависания → loop (7), интеграция (8). ✔
- Портируемость (python3 stdlib, mkdir-lock, sha через python) → 5,7; зависимости проверяет preflight (9). ✔
- Финальный отчёт → summary (4), loop summary.txt (7), команда шаг 7 (10). ✔
- Edge: in_progress resume (2,6), lock (7), needs-human/skipped исключены (2,8), плана нет/неоднозначен (9). ✔

**Placeholder scan:** код полный в каждом шаге; плейсхолдеров `TODO`/«сделать потом» нет. ✔

**Type consistency:** `Phase` поля едины во всех тасках; CLI-команды (`next`/`set-status`/`add-marker`/
`summary`/`phases`/`complete`) совпадают между `circle_plan.py` (4), loop (7), executor-prompt (6),
command (10). Коды возврата `run_phase` (0/2/3/4) согласованы между реализацией (5) и циклом (7). ✔
