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
