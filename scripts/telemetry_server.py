#!/usr/bin/env python3
"""circle-skill: приёмник телеметрии эффективности прогонов.

Живёт в контейнере-базе плагина (демонизирован systemd, переживает рестарт). Принимает от
плагинов-в-проектах готовый безопасный JSON POST'ом с bearer-токеном и складывает в локальный
store под gitignore. Потребитель store — LLM-аналитик по команде владельца.

Защита — многослойная, приёмник НЕ доверяет проводу:
  1) bearer-токен (constant-time сравнение) — иначе 401;
  2) кап на размер тела — иначе 413;
  3) JSON-парс + формальная валидация опаковых полей (run_uuid/hash-иды) — иначе 400;
  4) повторный fail-closed scrub_record (тот же, что на продюсере) — иначе 400;
  5) дедуп по run_uuid — повторная доставка идемпотентна (200, не плодит дубли).

Только stdlib (http.server). Порт по умолчанию 3000 / bind 0.0.0.0 — проксируется публичным
preview-URL контейнера, доступен и внешним машинам, и on-platform.
"""
import glob
import hmac
import importlib.util
import json
import os
import re
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

# scrub_record из ядра — единый источник правды по «что считается безопасным».
_SPEC = importlib.util.spec_from_file_location(
    "circle_telemetry", Path(__file__).resolve().parent / "circle_telemetry.py"
)
_tele = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(_tele)

MAX_BODY = 256 * 1024  # 256 KiB — запись прогона на порядки меньше; кап от abuse.

_UUID_RE = re.compile(r"\A[0-9a-f]{12}\Z")
_IDENT_RE = re.compile(r"\A([0-9a-f]{16}|anon)\Z")


def _valid_envelope(rec):
    """Формальная валидация опаковых полей до записи (в дополнение к scrub)."""
    return (
        isinstance(rec, dict)
        and isinstance(rec.get("schema_version"), str)
        and isinstance(rec.get("run_uuid"), str) and _UUID_RE.match(rec["run_uuid"])
        and isinstance(rec.get("machine_id"), str) and _IDENT_RE.match(rec["machine_id"])
        and isinstance(rec.get("plan_id"), str) and _IDENT_RE.match(rec["plan_id"])
    )


class _Handler(BaseHTTPRequestHandler):
    server_version = "circle-telemetry/1"

    def _reply(self, code, payload):
        body = json.dumps(payload).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _authed(self):
        want = "Bearer " + self.server.token
        got = self.headers.get("Authorization", "")
        return hmac.compare_digest(got, want)

    def log_message(self, fmt, *args):
        # Ноль сырья в логах приёмника (тела не логируем никогда). Строка доступа — в stderr.
        pass

    def do_GET(self):
        if self.path != "/health":
            return self._reply(404, {"error": "not_found"})
        if not self._authed():
            return self._reply(401, {"error": "unauthorized"})
        self._reply(200, {"ok": True})

    def do_POST(self):
        if self.path != "/ingest":
            return self._reply(404, {"error": "not_found"})
        if not self._authed():
            return self._reply(401, {"error": "unauthorized"})
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            return self._reply(400, {"error": "bad_length"})
        if length <= 0 or length > MAX_BODY:
            return self._reply(413, {"error": "too_large"})
        raw = self.rfile.read(length)
        try:
            rec = json.loads(raw.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            return self._reply(400, {"error": "bad_json"})
        if not _valid_envelope(rec):
            return self._reply(400, {"error": "bad_envelope"})
        if not _tele.scrub_record(rec):
            return self._reply(400, {"error": "scrub_failed"})
        # дедуп по run_uuid — идемпотентно
        store = self.server.store_dir
        os.makedirs(store, exist_ok=True)
        uuid = rec["run_uuid"]
        if glob.glob(os.path.join(store, "*-%s.json" % uuid)):
            return self._reply(200, {"ok": True, "duplicate": True})
        name = "%s-%s-%s.json" % (rec["machine_id"], rec["plan_id"], uuid)
        tmp = os.path.join(store, name + ".tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(rec, f, ensure_ascii=False, indent=2)
        os.replace(tmp, os.path.join(store, name))
        self._reply(201, {"ok": True})


class TelemetryServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, addr, token, store_dir):
        super().__init__(addr, _Handler)
        self.token = token
        self.store_dir = store_dir


def make_server(host, port, token, store_dir):
    """Фабрика — для тестов (порт 0 → эфемерный) и main()."""
    return TelemetryServer((host, port), token, store_dir)


def main(argv=None):
    import sys

    token = os.environ.get("CIRCLE_TELEMETRY_TOKEN", "")
    if not token:
        print("CIRCLE_TELEMETRY_TOKEN не задан — отказ стартовать (нужен bearer)", file=sys.stderr)
        return 2
    store = os.environ.get("CIRCLE_TELEMETRY_STORE", "/workspace/.telemetry/runs")
    host = os.environ.get("CIRCLE_TELEMETRY_HOST", "0.0.0.0")
    port = int(os.environ.get("CIRCLE_TELEMETRY_PORT", "3000"))
    os.makedirs(store, exist_ok=True)
    srv = make_server(host, port, token, store)
    print("circle-telemetry: слушаю %s:%d, store=%s" % (host, port, store), file=sys.stderr)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
