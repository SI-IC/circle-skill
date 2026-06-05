#!/usr/bin/env python3
"""circle-skill: парсинг фазового плана, выбор фазы, статусы, сводка."""

import re
from collections import Counter
from dataclasses import dataclass, field

HEADING_RE = re.compile(r"^##\s+Фаза\s+(\S+)\s+[—-]\s+(.+?)\s*$")
MARKER_RE = re.compile(r"<!--\s*circle:\s*(.*?)\s*-->")
JOURNAL_RE = re.compile(r"^##\s+Журнал\s*$")
_MARKER_SEARCH_LINES = 5  # маркер должен быть в пределах N строк после заголовка фазы

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
    m = re.search(r"status=([\w-]+)", inner)
    d["status"] = m.group(1) if m else "pending"
    m = re.search(r"order=(-?\d+)", inner)
    d["order"] = int(m.group(1)) if m else 0
    m = re.search(r"deps=\[([^\]]*)\]", inner)
    d["deps"] = [x.strip() for x in m.group(1).split(",") if x.strip()] if m else []
    m = re.search(r"autonomy=([\w-]+)", inner)
    d["autonomy"] = m.group(1) if m else "auto"
    m = re.search(r'obstacle="((?:[^"\\]|\\.)*)"', inner)
    d["obstacle"] = m.group(1).replace('\\"', '"') if m else ""
    return d


def parse_phases(text: str) -> list:
    lines = text.splitlines()
    phases = []
    for i, line in enumerate(lines):
        hm = HEADING_RE.match(line)
        if not hm:
            continue
        ph = Phase(id=hm.group(1), title=hm.group(2), heading_line=i)
        for j in range(i + 1, min(i + 1 + _MARKER_SEARCH_LINES, len(lines))):
            if HEADING_RE.match(lines[j]):
                break
            mm = MARKER_RE.search(lines[j])
            if mm:
                v = _parse_marker(mm.group(1))
                ph.status, ph.order = v["status"], v["order"]
                ph.deps, ph.autonomy, ph.obstacle = (
                    v["deps"],
                    v["autonomy"],
                    v["obstacle"],
                )
                ph.marker_line = j
                break
        phases.append(ph)
    return phases


def journal_section(text: str) -> str:
    """Тело секции «## Журнал» (без самого заголовка): от неё до следующего
    заголовка уровня `## ` или конца файла. Пусто, если секции нет.
    Записи фаз — уровня `###`, поэтому остаются внутри. Это дешёвый дайджест
    для следующей фазы: что построено и какие предупреждения оставили предыдущие."""
    lines = text.splitlines()
    start = next((i + 1 for i, ln in enumerate(lines) if JOURNAL_RE.match(ln)), None)
    if start is None:
        return ""
    end = next(
        (j for j in range(start, len(lines)) if lines[j].startswith("## ")), len(lines)
    )
    return "\n".join(lines[start:end]).strip()


def _dep_done(by_id, dep):
    # Зависимость удовлетворена только статусом done. Если предшественник skipped/blocked,
    # зависимая фаза намеренно не выбирается (остаётся pending → попадёт в сводку).
    p = by_id.get(dep)
    return p is not None and p.status == DONE


def select_next(phases):
    by_id = {p.id: p for p in phases}
    inprog = [p for p in phases if p.status == "in_progress"]
    if inprog:
        return sorted(inprog, key=lambda p: (p.order, p.id))[0]
    eligible = [
        p
        for p in phases
        if p.status == "pending"
        and p.autonomy == "auto"
        and all(_dep_done(by_id, d) for d in p.deps)
    ]
    if not eligible:
        return None
    # order — авторитетный ключ сортировки; при равном order id сравнивается лексикографически.
    return sorted(eligible, key=lambda p: (p.order, p.id))[0]


def is_complete(phases):
    """True, когда нет авто-подходящей фазы для следующего шага.
    Это НЕ значит, что все фазы done: план, где остаток — blocked/skipped/needs-human,
    тоже complete (циклу больше нечего делать автономно)."""
    return select_next(phases) is None


def _render_marker(status, order, deps, autonomy, obstacle):
    ob = obstacle.replace('"', '\\"')
    return (
        f"<!-- circle: status={status} order={order} "
        f'deps=[{",".join(deps)}] autonomy={autonomy} obstacle="{ob}" -->'
    )


def _find(phases, phase_id):
    for p in phases:
        if p.id == phase_id:
            return p
    raise KeyError(f"фаза {phase_id} не найдена")


def _reassemble(raw, original_text):
    sep = "\r\n" if "\r\n" in original_text else "\n"
    out = sep.join(raw)
    if original_text.endswith("\n"):
        out += sep
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


def add_marker(
    text, phase_id, status="pending", order=0, deps=None, autonomy="auto", obstacle=""
):
    deps = deps or []
    raw = text.splitlines()
    t = _find(parse_phases(text), phase_id)
    marker = _render_marker(status, order, deps, autonomy, obstacle)
    if t.marker_line >= 0:
        raw[t.marker_line] = marker
    else:
        raw.insert(t.heading_line + 1, marker)
    return _reassemble(raw, text)


def summary(phases):
    counts = Counter(p.status for p in phases)

    def lst(s):
        return [
            {"id": p.id, "title": p.title, "obstacle": p.obstacle}
            for p in phases
            if p.status == s
        ]

    return {
        "total": len(phases),
        "counts": dict(counts),
        "done": lst("done"),
        "pending": lst("pending"),
        "in_progress": lst("in_progress"),
        "blocked": lst("blocked"),
        "skipped": lst("skipped"),
        "needs_human": [
            {"id": p.id, "title": p.title}
            for p in phases
            if p.autonomy == "needs-human" and p.status != "done"
        ],
        "complete": is_complete(phases),
    }


def _phase_dict(p):
    return {
        "id": p.id,
        "title": p.title,
        "status": p.status,
        "order": p.order,
        "deps": p.deps,
        "autonomy": p.autonomy,
        "obstacle": p.obstacle,
    }


def _read(path):
    with open(path, encoding="utf-8") as f:
        return f.read()


def _write_file(path, text):
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)


def main(argv=None):
    import argparse
    import json
    import sys

    ap = argparse.ArgumentParser(prog="circle_plan")
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name in ("next", "complete", "summary", "phases", "journal"):
        sp = sub.add_parser(name)
        sp.add_argument("plan")
        if name in ("summary", "phases"):
            sp.add_argument("--json", action="store_true")
    sp = sub.add_parser("set-status")
    sp.add_argument("plan")
    sp.add_argument("id")
    sp.add_argument("status")
    sp.add_argument("--obstacle", default=None)
    sp = sub.add_parser("add-marker")
    sp.add_argument("plan")
    sp.add_argument("id")
    sp.add_argument("--status", default="pending")
    sp.add_argument("--order", type=int, default=0)
    sp.add_argument("--deps", default="")
    sp.add_argument("--autonomy", default="auto")
    sp.add_argument("--obstacle", default="")
    a = ap.parse_args(argv)

    try:
        text = _read(a.plan)  # один раз: согласованный снимок для всех команд чтения
        phases = parse_phases(text)
        if a.cmd == "next":
            nxt = select_next(phases)
            print("NONE" if nxt is None else f"{nxt.id}\t{nxt.title}")
            return 0
        if a.cmd == "complete":
            return 0 if is_complete(phases) else 1
        if a.cmd == "journal":
            body = journal_section(text)
            if body:
                print(body)
            return 0
        if a.cmd == "summary":
            s = summary(phases)
            if getattr(a, "json", False):
                print(json.dumps(s, ensure_ascii=False, indent=2))
            else:
                print(
                    f"Всего фаз: {s['total']}  |  "
                    + "  ".join(f"{k}={v}" for k, v in sorted(s["counts"].items()))
                )
                for b in s["blocked"]:
                    print(f"  blocked  {b['id']} — {b['title']}: {b['obstacle']}")
                for b in s["skipped"]:
                    print(f"  skipped  {b['id']} — {b['title']}")
            return 0
        if a.cmd == "phases":
            data = [_phase_dict(p) for p in phases]
            print(
                json.dumps(data, ensure_ascii=False, indent=2)
                if a.json
                else "\n".join(
                    f"{p['id']}\t{p['status']}\t{p['autonomy']}\t{p['title']}"
                    for p in data
                )
            )
            return 0
        if a.cmd == "set-status":
            _write_file(
                a.plan, set_status(_read(a.plan), a.id, a.status, obstacle=a.obstacle)
            )
            return 0
        if a.cmd == "add-marker":
            deps = [x.strip() for x in a.deps.split(",") if x.strip()]
            _write_file(
                a.plan,
                add_marker(
                    _read(a.plan),
                    a.id,
                    status=a.status,
                    order=a.order,
                    deps=deps,
                    autonomy=a.autonomy,
                    obstacle=a.obstacle,
                ),
            )
            return 0
        return 2
    except (FileNotFoundError, KeyError, ValueError) as e:
        print(f"circle_plan: ошибка: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    import sys

    sys.exit(main())
