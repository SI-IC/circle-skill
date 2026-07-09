#!/usr/bin/env python3
"""circle-skill: сбор безопасной структурной статистики эффективности прогонов.

Гарантия приватности — СТРУКТУРНАЯ: запись собирается из фиксированной схемы, значения —
только числа/булевы/enum из закрытых словарей/HMAC-хеши. Свободного текста, путей, кода,
имён проекта/фаз в записи нет по построению. Канала самоотчёта от LLM-сессии НЕТ — весь сбор
детерминированный (bash+git+эта функция), чтобы работа плагина в проектах не тратила лишних
токенов. Единственное «дорогое» — финальная строка «отправлено/не отправлено» печатается циклом.

Транспорт: клиент (`send`) шлёт готовый JSON HTTP-POST'ом на приёмник (`telemetry_server.py`)
с bearer-токеном; приёмник живёт в контейнере-базе плагина. Дизайн: docs/superpowers/specs/.
"""
import hashlib
import hmac
import json
import os
import re
import socket
import uuid

SCHEMA_VERSION = "1"

STOP_REASONS = frozenset({"complete", "no-progress", "hang", "crash", "error", "stuck"})
OUTCOMES = frozenset({"done", "blocked", "no-change", "skipped", "crash", "error"})
STATUS_KINDS = frozenset({"pending", "in_progress", "done", "blocked", "skipped"})
AUTONOMY_KINDS = frozenset({"auto", "needs-human"})

# Заголовок фазы — как HEADING_RE в circle_plan.py (допускает em-dash И дефис).
_HEADING_RE = re.compile(r"^##\s+Фаза\s+(\S+)\s+[—-]\s+(.+?)\s*$")
_MANIFEST_RE = re.compile(r"файловый манифест", re.IGNORECASE)
_MAP_RE = re.compile(r"^##\s+Карта кодовой базы", re.IGNORECASE | re.MULTILINE)
_BULLET_PATH_RE = re.compile(r"^\s*[-*]\s+`([^`]+)`")
_STRING_RE = re.compile(r"[0-9a-z._-]{1,40}")


# --- идентификаторы -----------------------------------------------------------

def ident(value, salt):
    """HMAC-SHA256(salt, value), первые 16 hex. Без соли → 'anon' (теряем кросс-машинную
    группировку, но не течём и не даём брутфорс низкоэнтропийного hostname/имени плана)."""
    if not salt:
        return "anon"
    return hmac.new(
        salt.encode("utf-8"), value.encode("utf-8"), hashlib.sha256
    ).hexdigest()[:16]


# --- гейт-примитивы -----------------------------------------------------------

def check_enum(value, vocab):
    """value, если строка ∈ vocab; иначе None → поле дропается гейтом."""
    return value if isinstance(value, str) and value in vocab else None


def clamp_int(value, lo, hi):
    """Целое, зажатое в [lo,hi]. Нецелое/None → lo. Режет ковровый канал через магнитуду."""
    try:
        n = int(value)
    except (TypeError, ValueError):
        return lo
    return max(lo, min(hi, n))


def string_ok(s):
    """True только для безопасной строки (hex/enum/semver/anon). Ловит путь, email, пробел,
    кавычку, длинный текст — рекурсивный бэкстоп поверх типизированной сборки."""
    return isinstance(s, str) and bool(_STRING_RE.fullmatch(s))


# --- парс плана (только счётчики; пути живут и умирают в процессе) -------------

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
    end = next(
        (j for j in range(start, len(lines)) if lines[j].startswith("## ")), len(lines)
    )
    return "\n".join(lines[start:end])


def manifest_paths(text, phase_id):
    """Множество путей из секции «Файловый манифест» тела фазы. Только для пересечения
    в памяти — наружу уходит лишь количество, пути не сериализуются."""
    body = phase_body(text, phase_id)
    if not body:
        return set()
    lines = body.splitlines()
    sec = next(
        (i for i, ln in enumerate(lines) if ln.startswith("#") and _MANIFEST_RE.search(ln)),
        None,
    )
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


def _journal_bytes(text):
    """Длина секции «## Журнал» (от заголовка до следующего «## » или конца), в байтах.
    Только размер — сам текст журнала не сериализуется."""
    lines = text.splitlines()
    start = next(
        (i + 1 for i, ln in enumerate(lines) if ln.startswith("## Журнал")), None
    )
    if start is None:
        return 0
    end = next(
        (j for j in range(start, len(lines)) if lines[j].startswith("## ")), len(lines)
    )
    return len("\n".join(lines[start:end]).strip().encode("utf-8"))


# --- staging пофазных скелетов ------------------------------------------------

def _run_stats_dir(work):
    d = os.path.join(work, "run-stats")
    os.makedirs(d, exist_ok=True)
    return d


def _append_line(path, line):
    with open(path, "a", encoding="utf-8") as f:
        f.write(line + "\n")


def record_phase(work, plan_text, phase_id, *, ordinal, attempts, duration_s, outcome,
                 plan_changed, committed, deps_count, autonomy, subphases_added,
                 touched_paths):
    """Детерминированный скелет фазы: счётчики покрытия манифеста (пересечение в памяти),
    длительность, попытки, флаги. Дописывает JSON-строку в <work>/run-stats/phases.jsonl."""
    declared = manifest_paths(plan_text, phase_id)
    touched = set(touched_paths)
    ratio = round(len(touched & declared) / len(touched), 3) if touched else 0.0
    rec = {
        "ordinal": clamp_int(ordinal, 0, 100000),
        "attempts": clamp_int(attempts, 0, 1000),
        "duration_s": clamp_int(duration_s, 0, 10 ** 7),
        "outcome": check_enum(outcome, OUTCOMES),
        "plan_changed": bool(plan_changed),
        "committed": bool(committed),
        "deps_count": clamp_int(deps_count, 0, 1000),
        "autonomy": check_enum(autonomy, AUTONOMY_KINDS),
        "subphases_added": clamp_int(subphases_added, 0, 1000),
        "files_changed": len(touched),
        "manifest_declared": len(declared),
        "files_off_manifest": len(touched - declared),
        "manifest_declared_untouched": len(declared - touched),
        "coverage_ratio": ratio,
        "journal_digest_bytes": _journal_bytes(plan_text),
    }
    _append_line(os.path.join(_run_stats_dir(work), "phases.jsonl"),
                 json.dumps(rec, ensure_ascii=False))
    return rec


# --- сборка записи прогона + fail-closed гейт ---------------------------------

def scrub_record(rec):
    """Рекурсивный бэкстоп: любое строковое значение обязано пройти string_ok. Провал =
    баг гейта или инъекция → дроп ВСЕЙ записи (fail-closed). Числа/булевы/None — ок."""
    if isinstance(rec, bool) or rec is None:
        return True
    if isinstance(rec, (int, float)):
        return True
    if isinstance(rec, str):
        return string_ok(rec)
    if isinstance(rec, dict):
        return all(isinstance(k, str) for k in rec) and all(scrub_record(v) for v in rec.values())
    if isinstance(rec, list):
        return all(scrub_record(v) for v in rec)
    return False  # неизвестный тип → дроп


def _drop_none(d):
    return {k: v for k, v in d.items() if v is not None}


def build_run_record(*, plan_text, plugin_version, machine, plan_slug, salt, stop_reason,
                     run_wall_s, sessions_total, phases_total, status_counts, phase_recs):
    """Собирает ОДИН conflict-free словарь прогона из фиксированной схемы. Обязательный enum
    вне словаря → None (fail-closed). Скраб не прошёл → None. Иначе — готовая безопасная запись."""
    stop = check_enum(stop_reason, STOP_REASONS)
    if stop is None:
        return None
    sc = {
        k: clamp_int(v, 0, 100000)
        for k, v in status_counts.items()
        if check_enum(k, STATUS_KINDS)
    }
    phases = []
    for pr in phase_recs:
        phases.append(_drop_none({k: v for k, v in pr.items()}))
    rec = {
        "schema_version": SCHEMA_VERSION,
        "plugin_version": plugin_version if string_ok(plugin_version) else "0",
        "machine_id": ident(machine, salt),
        "plan_id": ident(plan_slug, salt),
        "run_uuid": uuid.uuid4().hex[:12],
        "stop_reason": stop,
        "run_wall_s": clamp_int(run_wall_s, 0, 10 ** 8),
        "sessions_total": clamp_int(sessions_total, 0, 100000),
        "phases_total": clamp_int(phases_total, 0, 100000),
        "status_counts": sc,
        "has_codebase_map": has_codebase_map(plan_text),
        "phases": phases,
    }
    return rec if scrub_record(rec) else None


def _load_phase_recs(work):
    pj = os.path.join(work, "run-stats", "phases.jsonl")
    recs = []
    if os.path.exists(pj):
        with open(pj, encoding="utf-8") as f:
            for ln in f:
                ln = ln.strip()
                if not ln:
                    continue
                try:
                    recs.append(json.loads(ln))
                except ValueError:
                    continue
    return recs


def _atomic_write(path, data):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(data)
    os.replace(tmp, path)


# --- CLI ----------------------------------------------------------------------

def _parse_status_counts(csv):
    counts = {}
    for kv in csv.split(","):
        if "=" in kv:
            k, v = kv.split("=", 1)
            counts[k.strip()] = v.strip()
    return counts


def main(argv=None):
    import argparse
    import sys

    ap = argparse.ArgumentParser(prog="circle_telemetry")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("record-phase")
    p.add_argument("plan")
    p.add_argument("phase_id")
    p.add_argument("--work", required=True)
    p.add_argument("--ordinal", default=0)
    p.add_argument("--attempts", default=0)
    p.add_argument("--duration-s", default=0)
    p.add_argument("--outcome", default="")
    p.add_argument("--plan-changed", default="0")
    p.add_argument("--committed", default="0")
    p.add_argument("--deps-count", default=0)
    p.add_argument("--autonomy", default="auto")
    p.add_argument("--subphases-added", default=0)

    p = sub.add_parser("build-run")
    p.add_argument("plan")
    p.add_argument("--work", required=True)
    p.add_argument("--out-dir", required=True)
    p.add_argument("--plugin-version", default="0")
    p.add_argument("--stop-reason", required=True)
    p.add_argument("--run-wall-s", default=0)
    p.add_argument("--sessions-total", default=0)
    p.add_argument("--phases-total", default=0)
    p.add_argument("--status-counts", default="")

    a = ap.parse_args(argv)
    salt = os.environ.get("CIRCLE_TELEMETRY_SALT") or None

    if a.cmd == "record-phase":
        touched = [ln.strip() for ln in sys.stdin.read().splitlines() if ln.strip()]
        with open(a.plan, encoding="utf-8") as f:
            plan_text = f.read()
        record_phase(
            a.work, plan_text, a.phase_id,
            ordinal=a.ordinal, attempts=a.attempts, duration_s=a.duration_s,
            outcome=a.outcome,
            plan_changed=(str(a.plan_changed) not in ("0", "", "false", "False")),
            committed=(str(a.committed) not in ("0", "", "false", "False")),
            deps_count=a.deps_count, autonomy=a.autonomy,
            subphases_added=a.subphases_added, touched_paths=touched,
        )
        return 0

    if a.cmd == "build-run":
        with open(a.plan, encoding="utf-8") as f:
            plan_text = f.read()
        rec = build_run_record(
            plan_text=plan_text, plugin_version=a.plugin_version,
            machine=socket.gethostname(), plan_slug=os.path.basename(a.plan),
            salt=salt, stop_reason=a.stop_reason, run_wall_s=a.run_wall_s,
            sessions_total=a.sessions_total, phases_total=a.phases_total,
            status_counts=_parse_status_counts(a.status_counts),
            phase_recs=_load_phase_recs(a.work),
        )
        if rec is None:
            print("DROPPED", file=sys.stderr)
            return 0
        os.makedirs(a.out_dir, exist_ok=True)
        name = "%s-%s-%s.json" % (rec["machine_id"], rec["plan_id"], rec["run_uuid"])
        _atomic_write(os.path.join(a.out_dir, name),
                      json.dumps(rec, ensure_ascii=False, indent=2))
        print(name)
        return 0

    return 2


if __name__ == "__main__":
    raise SystemExit(main())
