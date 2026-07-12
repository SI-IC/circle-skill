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

SCHEMA_VERSION = "4"  # v4: +manifest_miss_count на фазу (промахи манифеста, self-report сессии)

STOP_REASONS = frozenset({"complete", "no-progress", "hang", "crash", "error", "stuck"})
OUTCOMES = frozenset({"done", "blocked", "no-change", "skipped", "crash", "error"})
STATUS_KINDS = frozenset({"pending", "in_progress", "done", "blocked", "skipped"})
AUTONOMY_KINDS = frozenset({"auto", "needs-human"})

_MAP_RE = re.compile(r"^##\s+Карта кодовой базы", re.IGNORECASE | re.MULTILINE)
_STRING_RE = re.compile(r"[0-9a-z._-]{1,40}")
_RUN_UUID_RE = re.compile(r"\A[0-9a-f]{12}\Z")


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


def _parse_context_pct(value):
    """Пик потребления контекста, % → int в [0,100] или None. Пусто/'?'/нечисло → None
    (честное «неизвестно», не 0 — иначе распознанный-как-0 и нераспознанный слились бы)."""
    if value is None:
        return None
    s = str(value).strip()
    if not s or s == "?":
        return None
    try:
        return max(0, min(100, int(s)))
    except ValueError:
        return None


_MISS_RE = re.compile(r"^miss\((\d+)\)$", re.IGNORECASE)


def _parse_miss_count(value):
    """Строгий разбор self-report токена промахов манифеста → int ≥0 или None.
    Принимает РОВНО `ok` (→0) или `miss(N)` (→N), регистронезависимо, без хвоста. Всё прочее
    (пусто / проза / `miss(2) — foo` / `miss()` / голое число) → None = «сессия не отчиталась»,
    отличимо от отчитанного 0 (`ok`). Строгость намеренна: рыхлый разбор склеил бы цифры из
    пояснения в правдоподобное фейк-число (`miss(2) 2 файла` → 22) — а фейк хуже честного None.
    Токен эмитит сама сессия (знает свой манифест), НЕ path-diff (declared-vs-touched по прозе
    манифеста был в схеме v1 — всегда 0, регэксп не парсил прозу; удалён)."""
    if value is None:
        return None
    s = str(value).strip()
    if s.lower() == "ok":
        return 0
    m = _MISS_RE.match(s)
    if not m:
        return None
    return max(0, min(100000, int(m.group(1))))


# --- парс плана (только счётчики; пути живут и умирают в процессе) -------------

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
                 touched_paths, context_pct=None, manifest_misses=None):
    """Детерминированный скелет фазы: длительность, попытки, число изменённых файлов, флаги.
    Дописывает JSON-строку в <work>/run-stats/phases.jsonl.

    context_pct — пик потребления контекстного окна сессией, % (0..100); None если статус-бар
    не распознан. Отвечает на «фаза пухлая или просто долгая?»: duration_s меряет время, а это —
    именно нагрузку на контекст, независимую ось. Важно при разборе: None при БОЛЬШОМ duration_s
    (живой TUI рисует бар непрерывно) ⇒ извлечение сломано (дрейф формата бара), а не низкая
    нагрузка — образец нераспознанного кадра ищи в loop.log по `CIRCLE_CTX_UNPARSED`.

    manifest_misses — сырой self-report токен сессии о промахах манифеста (`ok`/`miss(N)`; файл
    переехал/отсутствовал/пришлось трогать файл вне манифеста) — сигнал стоимости «въезда» в фазу.
    Строго разбирается в int ≥0 или None (не отчиталась, отличимо от 0). См. _parse_miss_count."""
    touched = set(touched_paths)
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
        "journal_digest_bytes": _journal_bytes(plan_text),
        "context_pct": _parse_context_pct(context_pct),
        "manifest_miss_count": _parse_miss_count(manifest_misses),
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
                     run_wall_s, sessions_total, phases_total, status_counts, phase_recs,
                     run_uuid=None):
    """Собирает ОДИН conflict-free словарь прогона из фиксированной схемы. Обязательный enum
    вне словаря → None (fail-closed). Скраб не прошёл → None. Иначе — готовая безопасная запись.

    Семантика для аналитика: `phases` — фазы, ИСПОЛНЁННЫЕ в этом прогоне (по одной записи из
    phases.jsonl), тогда как `phases_total`/`status_counts` — финальное состояние ВСЕГО плана.
    Потому `len(phases) < phases_total` — норма, не потеря данных: уже-`done` до старта и `skipped`
    фазы сессиями не исполняются. Простой прогона выводится как `run_wall_s − Σ duration_s`."""
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
        "run_uuid": run_uuid if (isinstance(run_uuid, str) and _RUN_UUID_RE.match(run_uuid))
                    else uuid.uuid4().hex[:12],
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


# --- клиент: отправка на приёмник (best-effort, ноль LLM-токенов) -------------
#
# Модель ledger'а: build-run кладёт готовый JSON в <work>/run-stats/outbox/. send пытается
# доставить всё из outbox; доставленное (2xx, дедуп на сервере) удаляется из outbox и пишется
# в sent.log. Недоставленное остаётся в outbox → догон при следующем прогоне или командой.

def _outbox(work):
    return os.path.join(work, "run-stats", "outbox")


def _url_ok(url):
    """Токен/данные шлём только по https ЛИБО http на loopback (локальный тест/прокси на той же
    машине). Голый http на внешний хост → отказ: bearer и запись не должны идти в открытом виде."""
    from urllib.parse import urlparse

    try:
        u = urlparse(url or "")
    except ValueError:
        return False
    if u.scheme == "https":
        return True
    return u.scheme == "http" and u.hostname in ("127.0.0.1", "localhost", "::1")


def _post(url, token, data, timeout):
    import urllib.error
    import urllib.request

    req = urllib.request.Request(url, data=data, method="POST")
    req.add_header("Authorization", "Bearer " + token)
    req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, None
    except urllib.error.HTTPError as e:
        return e.code, ("401" if e.code == 401 else "http-%d" % e.code)
    except OSError:
        return None, "нет-связи"


def ping(url, token, timeout=10):
    """GET /health с bearer. (ok, reason). Для команды активации в проекте."""
    import urllib.error
    import urllib.request

    if not url or not token:
        return False, "не-настроено"
    if not _url_ok(url):
        return False, "небезопасный-url"
    req = urllib.request.Request(url.rstrip("/") + "/health", method="GET")
    req.add_header("Authorization", "Bearer " + token)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return (r.status == 200), (None if r.status == 200 else "http-%d" % r.status)
    except urllib.error.HTTPError as e:
        return False, ("401" if e.code == 401 else "http-%d" % e.code)
    except OSError:
        return False, "нет-связи"


def send_outbox(work, url, token, timeout=10):
    """Догоняет всё неотправленное из outbox. Возвращает {sent, failed, reason}. Best-effort:
    сетевой/auth-сбой не бросает исключение — файлы остаются в outbox до следующего раза."""
    import glob

    files = sorted(glob.glob(os.path.join(_outbox(work), "*.json")))
    if not files:
        return {"sent": 0, "failed": 0, "reason": "нет-данных"}
    if not url or not token:
        return {"sent": 0, "failed": len(files), "reason": "не-настроено"}
    if not _url_ok(url):  # не шлём токен по plain-http на внешний хост
        return {"sent": 0, "failed": len(files), "reason": "небезопасный-url"}
    endpoint = url.rstrip("/") + "/ingest"
    sent = failed = 0
    reason = "ok"
    for fp in files:
        with open(fp, "rb") as f:
            data = f.read()
        status, err = _post(endpoint, token, data, timeout)
        if status in (200, 201):
            os.remove(fp)
            _append_line(os.path.join(work, "run-stats", "sent.log"), os.path.basename(fp))
            sent += 1
        elif status in (400, 413):
            # Приёмник отверг запись НАВСЕГДА (битая/слишком большая) — уводим из outbox в
            # rejected/, иначе она ретраилась бы каждый прогон и блокировала очередь (poison).
            rej = os.path.join(work, "run-stats", "rejected")
            os.makedirs(rej, exist_ok=True)
            os.replace(fp, os.path.join(rej, os.path.basename(fp)))
            failed += 1
            reason = "отклонено-приёмником"
        else:
            # 401 / нет-связи — транзиентно: файл остаётся в outbox до следующего раза.
            failed += 1
            reason = err or "ошибка"
    return {"sent": sent, "failed": failed, "reason": "ok" if failed == 0 else reason}


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
    p.add_argument("--context-pct", default="")  # пик контекста, %; пусто = неизвестно
    p.add_argument("--manifest-misses", default="")  # промахи манифеста; пусто = не отчиталась

    p = sub.add_parser("build-run")
    p.add_argument("plan")
    p.add_argument("--work", required=True)
    p.add_argument("--out-dir", required=True)
    p.add_argument("--plugin-version", default="0")
    p.add_argument("--stop-reason", required=True)
    p.add_argument("--run-wall-s", default=0)
    p.add_argument("--run-uuid", default="")
    p.add_argument("--sessions-total", default=0)
    p.add_argument("--phases-total", default=0)
    p.add_argument("--status-counts", default="")

    p = sub.add_parser("send")
    p.add_argument("--work", required=True)
    p.add_argument("--url", default=os.environ.get("CIRCLE_TELEMETRY_URL", ""))
    p.add_argument("--token", default=os.environ.get("CIRCLE_TELEMETRY_TOKEN", ""))

    p = sub.add_parser("activate")
    p.add_argument("--url", default=os.environ.get("CIRCLE_TELEMETRY_URL", ""))
    p.add_argument("--token", default=os.environ.get("CIRCLE_TELEMETRY_TOKEN", ""))

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
            context_pct=a.context_pct, manifest_misses=a.manifest_misses,
        )
        return 0

    if a.cmd == "build-run":
        with open(a.plan, encoding="utf-8") as f:
            plan_text = f.read()
        rec = build_run_record(
            plan_text=plan_text, plugin_version=a.plugin_version,
            machine=socket.gethostname(), plan_slug=os.path.basename(a.plan),
            salt=salt, stop_reason=a.stop_reason, run_wall_s=a.run_wall_s,
            run_uuid=(a.run_uuid or None),
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

    if a.cmd == "send":
        r = send_outbox(a.work, a.url, a.token)
        # строка статуса для финала цикла — единственное «дорогое» (и то печатает цикл, не LLM)
        print("отправлено=%d не_отправлено=%d причина=%s"
              % (r["sent"], r["failed"], r["reason"]))
        return 0

    if a.cmd == "activate":
        ok, reason = ping(a.url, a.token)
        if ok:
            print("активация: связь с приёмником есть, логирование включено")
            return 0
        print("активация: НЕ включено (причина: %s)" % (reason or "?"), file=sys.stderr)
        return 1

    return 2


if __name__ == "__main__":
    raise SystemExit(main())
