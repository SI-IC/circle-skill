"""E2E: цикл с включённой телеметрией реально отправляет запись на живой приёмник.

Проверяет всю цепочку врезки в circle-loop.sh: per-phase record → build-run → send → store.
Регресс «off по умолчанию» покрыт в test_loop_integration (телеметрия там не настроена).
"""
import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
LOOP = str(ROOT / "scripts" / "circle-loop.sh")
FAKE = str(ROOT / "tests" / "fixtures" / "fake_claude.py")

_spec = importlib.util.spec_from_file_location(
    "telemetry_server", ROOT / "scripts" / "telemetry_server.py"
)
srv_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(srv_mod)

TOKEN = "loop-e2e-token-123"
PLAN = (
    "# План тест\n\n"
    "## Карта кодовой базы\n- `plan.md` — план.\n\n"
    '## Фаза 1 — A\n<!-- circle: status=pending order=10 deps=[] autonomy=auto obstacle="" -->\n\n'
    '## Фаза 2 — B\n<!-- circle: status=pending order=20 deps=[1] autonomy=auto obstacle="" -->\n\n'
    "## Журнал\n"
)


class TestLoopTelemetry(unittest.TestCase):
    def setUp(self):
        self.store = tempfile.mkdtemp()
        self.srv = srv_mod.make_server("127.0.0.1", 0, TOKEN, self.store)
        self.url = "http://127.0.0.1:%d" % self.srv.server_address[1]
        self.t = threading.Thread(target=self.srv.serve_forever, daemon=True)
        self.t.start()

    def tearDown(self):
        self.srv.shutdown()
        self.srv.server_close()

    def _git_plan(self):
        d = tempfile.mkdtemp()
        for args in (["init", "-q"], ["config", "user.email", "t@t"], ["config", "user.name", "t"]):
            subprocess.run(["git", "-C", d, *args], check=True)
        p = os.path.join(d, "plan.md")
        open(p, "w", encoding="utf-8").write(PLAN)
        subprocess.run(["git", "-C", d, "add", "plan.md"], check=True)
        subprocess.run(["git", "-C", d, "commit", "-q", "-m", "init"], check=True)
        # .env проекта с конфигом телеметрии — цикл достаёт URL/токен именно отсюда
        with open(os.path.join(d, ".env"), "w") as f:
            f.write("CIRCLE_TELEMETRY_URL=%s\nCIRCLE_TELEMETRY_TOKEN=%s\nCIRCLE_TELEMETRY_SALT=e2esalt\n"
                    % (self.url, TOKEN))
        return p, d

    def _run(self, plan):
        env = dict(os.environ)
        env["FAKE_MODE"] = "done"
        env["CIRCLE_TIMEOUT"] = "30"
        env["CLAUDE_PLUGIN_ROOT"] = str(ROOT)
        wrapper = plan + ".claude.sh"
        with open(wrapper, "w") as f:
            f.write('#!/usr/bin/env bash\nexec "%s" "%s" "$@"\n' % (sys.executable, FAKE))
        os.chmod(wrapper, 0o755)
        env["CIRCLE_CLAUDE_BIN"] = wrapper
        # не наследуем возможный внешний конфиг телеметрии — берём только из .env проекта
        for k in ("CIRCLE_TELEMETRY_URL", "CIRCLE_TELEMETRY_TOKEN", "CIRCLE_TELEMETRY_SALT"):
            env.pop(k, None)
        return subprocess.run(["bash", LOOP, plan], env=env, capture_output=True, text=True, timeout=120)

    def test_run_sends_record_to_store(self):
        plan, d = self._git_plan()
        r = self._run(plan)
        self.assertEqual(r.returncode, 0, r.stderr)
        files = os.listdir(self.store)
        self.assertEqual(len(files), 1, "ожидали одну запись прогона в store")
        with open(os.path.join(self.store, files[0])) as f:
            rec = json.load(f)
        # структурная целостность + приватность
        self.assertEqual(rec["stop_reason"], "complete")
        self.assertTrue(rec["has_codebase_map"])
        self.assertEqual(rec["sessions_total"], 2)   # две auto-фазы
        self.assertGreaterEqual(len(rec["phases"]), 1)
        self.assertNotIn("plan.md", json.dumps(rec))  # ни одного пути/имени файла
        self.assertNotIn(d, json.dumps(rec))          # ни абсолютного пути проекта
        # статус-строка в сводке
        summary = open(os.path.join(os.path.dirname(plan), ".circle", "plan", "summary.txt")).read()
        self.assertIn("телеметрия: отправлено=1", summary)

    def test_restart_keeps_run_uuid_and_sessions(self):
        # Рестарт цикла на том же плане (work-dir сохранён): все фазы уже done → 0 новых сессий,
        # но стабильный run_uuid + overwrite-last дают ОДНУ запись, а sessions_total переживает
        # рестарт (не сбрасывается в 0). Регресс для hang→рестарт-случая.
        plan, d = self._git_plan()
        self.assertEqual(self._run(plan).returncode, 0)
        first = os.listdir(self.store)
        self.assertEqual(len(first), 1)
        self.assertEqual(self._run(plan).returncode, 0)  # рестарт
        second = os.listdir(self.store)
        self.assertEqual(len(second), 1, "overwrite-last: снимки склеились в одну запись")
        self.assertEqual(second[0], first[0], "run_uuid стабилен между рестартами")
        with open(os.path.join(self.store, second[0])) as f:
            rec = json.load(f)
        self.assertEqual(rec["sessions_total"], 2, "sessions_total пережил рестарт (не сброшен)")

    def test_offline_receiver_keeps_outbox_no_crash(self):
        # приёмник недоступен → send падает мягко, цикл завершается, запись ждёт в outbox
        plan, d = self._git_plan()
        # подменяем URL на заведомо мёртвый порт
        with open(os.path.join(d, ".env"), "w") as f:
            f.write("CIRCLE_TELEMETRY_URL=http://127.0.0.1:1\nCIRCLE_TELEMETRY_TOKEN=%s\n" % TOKEN)
        r = self._run(plan)
        self.assertEqual(r.returncode, 0, r.stderr)  # цикл не упал
        outbox = os.path.join(os.path.dirname(plan), ".circle", "plan", "run-stats", "outbox")
        self.assertEqual(len(os.listdir(outbox)), 1)  # запись сохранена для догона
        summary = open(os.path.join(os.path.dirname(plan), ".circle", "plan", "summary.txt")).read()
        self.assertIn("не_отправлено=1", summary)


if __name__ == "__main__":
    unittest.main()
