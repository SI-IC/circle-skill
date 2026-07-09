"""E2E клиентского скрипта telemetry-client.sh + регресс на баг экранирования команды.

Баг: логика жила inline в `!`bash -c '...'`` внутри commands/circle-telemetry.md; литеральные
одинарные кавычки в теле рвали обрамление, скилл писал/читал .env поломанно. Вынесли в скрипт.
"""
import importlib.util
import os
import re
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = str(ROOT / "scripts" / "telemetry-client.sh")
CMD_MD = ROOT / "commands" / "circle-telemetry.md"

_spec = importlib.util.spec_from_file_location(
    "telemetry_server", ROOT / "scripts" / "telemetry_server.py"
)
srv_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(srv_mod)

TOKEN = "script-e2e-token-abcdef"


class TestClientScript(unittest.TestCase):
    def setUp(self):
        self.store = tempfile.mkdtemp()
        self.srv = srv_mod.make_server("127.0.0.1", 0, TOKEN, self.store)
        self.url = "http://127.0.0.1:%d" % self.srv.server_address[1]
        self.t = threading.Thread(target=self.srv.serve_forever, daemon=True)
        self.t.start()
        self.repo = tempfile.mkdtemp()
        subprocess.run(["git", "-C", self.repo, "init", "-q"], check=True)

    def tearDown(self):
        self.srv.shutdown()
        self.srv.server_close()

    def _run(self, *args):
        env = {**os.environ, "CLAUDE_PLUGIN_ROOT": str(ROOT)}
        return subprocess.run(
            ["bash", SCRIPT, *args], cwd=self.repo, env=env,
            capture_output=True, text=True,
        )

    def _env_text(self):
        p = os.path.join(self.repo, ".env")
        return open(p).read() if os.path.exists(p) else ""

    def test_activate_writes_env_and_pings(self):
        r = self._run("activate", self.url, TOKEN)
        self.assertIn("CIRCLE_TELEMETRY_URL=%s" % self.url, self._env_text())
        self.assertIn("CIRCLE_TELEMETRY_TOKEN=%s" % TOKEN, self._env_text())
        self.assertIn("включено", r.stdout, r.stdout + r.stderr)
        # .env добавлен в .gitignore
        gi = open(os.path.join(self.repo, ".gitignore")).read()
        self.assertIn(".env", gi)

    def test_activate_no_args_reads_env(self):
        self._run("activate", self.url, TOKEN)        # сначала записали
        r = self._run("activate")                     # теперь без аргументов — читает .env
        self.assertIn("включено", r.stdout, r.stdout + r.stderr)

    def test_token_rotation_replaces_in_place(self):
        self._run("activate", self.url, "old-token-value")
        self._run("activate", self.url, TOKEN)        # новый токен
        env = self._env_text()
        self.assertEqual(env.count("CIRCLE_TELEMETRY_TOKEN="), 1)  # не дубль
        self.assertIn("CIRCLE_TELEMETRY_TOKEN=%s" % TOKEN, env)

    def test_bad_token_reports_reason_keeps_env(self):
        r = self._run("activate", self.url, "wrong-token")
        self.assertNotIn("включено", r.stdout)
        self.assertIn("401", r.stdout + r.stderr)

    def test_command_md_inline_invocation_parses(self):
        # Регресс на исходный баг: содержимое !`...` из команды должно ДОЙТИ до activate,
        # а не свалиться в else «использование» из-за поломанного экранирования.
        s = CMD_MD.read_text(encoding="utf-8")
        m = re.search(r"!`(.*?)`", s, re.S)
        self.assertIsNotNone(m)
        cmdline = m.group(1)
        env = {**os.environ, "CLAUDE_PLUGIN_ROOT": str(ROOT),
               "ARGUMENTS": "activate %s %s" % (self.url, TOKEN)}
        r = subprocess.run(["bash", "-c", cmdline], cwd=self.repo, env=env,
                           capture_output=True, text=True)
        self.assertNotIn("использование:", r.stdout)
        self.assertIn("включено", r.stdout, r.stdout + r.stderr)


if __name__ == "__main__":
    unittest.main()
