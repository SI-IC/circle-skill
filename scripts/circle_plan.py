#!/usr/bin/env python3
"""circle-skill: парсинг фазового плана, выбор фазы, статусы, сводка."""

import re
from dataclasses import dataclass, field

HEADING_RE = re.compile(r"^##\s+Фаза\s+(\S+)\s+[—-]\s+(.+?)\s*$")
MARKER_RE = re.compile(r"<!--\s*circle:\s*(.*?)\s*-->")

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
        for j in range(i + 1, min(i + 4, len(lines))):
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


def _dep_done(by_id, dep):
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
    return sorted(eligible, key=lambda p: (p.order, p.id))[0]


def is_complete(phases):
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
