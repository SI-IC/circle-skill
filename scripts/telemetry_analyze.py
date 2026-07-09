#!/usr/bin/env python3
"""circle-skill: учёт разобранных записей телеметрии (в контейнере-базе плагина).

Store с входящими записями и этот ledger лежат под gitignore (`.telemetry/`). `fresh` печатает,
какие записи ещё не проанализированы (чтобы LLM-аналитик не разбирал повторно), `mark` отмечает
разобранные с меткой времени. Данные для анализа — только числа/enum/хеши, см. telemetry_server.py.
"""
import json
import os
import re

_UUID_RE = re.compile(r"-([0-9a-f]{12})\.json\Z")


def _resolve():
    store = os.environ.get("CIRCLE_TELEMETRY_STORE", "/workspace/.telemetry/runs")
    ledger = os.environ.get(
        "CIRCLE_TELEMETRY_ANALYZED",
        os.path.join(os.path.dirname(store.rstrip("/")), "analyzed.json"),
    )
    return store, ledger


def load_ledger(ledger):
    if os.path.exists(ledger):
        try:
            with open(ledger, encoding="utf-8") as f:
                return json.load(f)
        except ValueError:
            return {}
    return {}


def _uuid(name):
    m = _UUID_RE.search(name)
    return m.group(1) if m else None


def fresh(store, ledger):
    """Имена записей из store, чьих run_uuid ещё нет в ledger."""
    seen = load_ledger(ledger)
    if not os.path.isdir(store):
        return []
    out = []
    for name in sorted(os.listdir(store)):
        if name.endswith(".json"):
            u = _uuid(name)
            if u and u not in seen:
                out.append(name)
    return out


def mark(store, ledger, names, now):
    """Отметить записи разобранными (run_uuid → iso-время). now передаётся снаружи (тестируемо)."""
    seen = load_ledger(ledger)
    for name in names:
        u = _uuid(name)
        if u:
            seen[u] = now
    os.makedirs(os.path.dirname(ledger) or ".", exist_ok=True)
    tmp = ledger + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(seen, f, ensure_ascii=False, indent=2, sort_keys=True)
    os.replace(tmp, ledger)
    return seen


def main(argv=None):
    import argparse
    from datetime import datetime, timezone

    ap = argparse.ArgumentParser(prog="telemetry_analyze")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("fresh")
    m = sub.add_parser("mark")
    m.add_argument("names", nargs="*", help="имена файлов; пусто → все свежие")
    a = ap.parse_args(argv)

    store, ledger = _resolve()
    if a.cmd == "fresh":
        for n in fresh(store, ledger):
            print(n)
        return 0
    if a.cmd == "mark":
        names = a.names or fresh(store, ledger)
        now = datetime.now(timezone.utc).isoformat(timespec="seconds")
        mark(store, ledger, names, now)
        print("отмечено разобранными: %d" % len(names))
        return 0
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
