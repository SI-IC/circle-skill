"""Тест guard'а .githooks/pre-commit — единственной защиты «токен телеметрии НЕ в VCS».

Гоняем реальный хук через git commit в одноразовых репозиториях (hooksPath → .githooks проекта).
"""
import os
import subprocess
import tempfile
import unittest
from pathlib import Path

HOOKS = str(Path(__file__).resolve().parent.parent / ".githooks")
REAL_TOKEN = "a1b2c3d4e5f6a7b8c9d0e1f2a3b4c5d6e7f8a9b0"  # 40 hex — как token_urlsafe


class TestPreCommitGuard(unittest.TestCase):
    def _repo(self):
        d = tempfile.mkdtemp()
        for args in (["init", "-q"], ["config", "core.hooksPath", HOOKS],
                     ["config", "user.email", "t@t"], ["config", "user.name", "t"]):
            subprocess.run(["git", "-C", d, *args], check=True)
        return d

    def _commit(self, d):
        subprocess.run(["git", "-C", d, "add", "-A", "-f"], check=True,
                       capture_output=True)
        return subprocess.run(["git", "-C", d, "commit", "-m", "x"],
                              capture_output=True, text=True).returncode

    def _write(self, d, name, content):
        with open(os.path.join(d, name), "w", encoding="utf-8") as f:
            f.write(content)

    def test_real_token_env_form_blocked(self):
        d = self._repo()
        self._write(d, "cfg.sh", "CIRCLE_TELEMETRY_TOKEN=%s\n" % REAL_TOKEN)
        self.assertNotEqual(self._commit(d), 0)

    def test_real_token_json_form_blocked(self):
        d = self._repo()
        self._write(d, "cfg.json", '{"CIRCLE_TELEMETRY_TOKEN": "%s"}\n' % REAL_TOKEN)
        self.assertNotEqual(self._commit(d), 0)

    def test_docs_placeholder_passes(self):
        d = self._repo()
        self._write(d, "README.md",
                    "| `CIRCLE_TELEMETRY_TOKEN` | .env | bearer |\n"
                    "Впиши: CIRCLE_TELEMETRY_TOKEN=<токен-из-контейнера>\n")
        self.assertEqual(self._commit(d), 0)

    def test_forced_env_file_blocked(self):
        d = self._repo()
        self._write(d, ".env", "CIRCLE_TELEMETRY_TOKEN=short\n")
        self.assertNotEqual(self._commit(d), 0)

    def test_local_token_value_blocked_anywhere(self):
        # реальный токен из .env, вставленный в другой файл (в любом синтаксисе) — check(3)
        d = self._repo()
        self._write(d, ".env", "CIRCLE_TELEMETRY_TOKEN=%s\n" % REAL_TOKEN)
        subprocess.run(["git", "-C", d, "rm", "--cached", "-q", "--ignore-unmatch", ".env"])
        # .env не коммитим (нет в индексе), но его значение просочилось в notes.txt
        self._write(d, "notes.txt", "секрет на память: %s\n" % REAL_TOKEN)
        subprocess.run(["git", "-C", d, "add", "notes.txt"], check=True)
        rc = subprocess.run(["git", "-C", d, "commit", "-m", "x"],
                            capture_output=True, text=True).returncode
        self.assertNotEqual(rc, 0)

    def test_clean_passes(self):
        d = self._repo()
        self._write(d, "app.py", "print('hello')\n")
        self.assertEqual(self._commit(d), 0)


if __name__ == "__main__":
    unittest.main()
