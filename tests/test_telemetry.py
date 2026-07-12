"""Юнит-тесты ядра circle_telemetry.py — безопасный сбор структурной статистики.

Ядро детерминированное: ноль свободного текста, значения — числа/булевы/enum из закрытых
словарей/HMAC-хеши. Канала самоотчёта от LLM-сессии НЕТ (вырезан ради нулевой токен-нагрузки).
"""
import hashlib
import importlib.util
import json
import os
import tempfile
import unittest
from pathlib import Path

_SPEC = importlib.util.spec_from_file_location(
    "circle_telemetry",
    Path(__file__).resolve().parent.parent / "scripts" / "circle_telemetry.py",
)
tele = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(tele)

_SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "circle_telemetry.py"

# Заголовок фазы канонично допускает и em-dash, и дефис (как HEADING_RE в circle_plan.py).
_PLAN = """# План

## Карта кодовой базы
- `scripts/circle_plan.py` — ядро.

## Фаза 1 — Первая
<!-- circle: status=done order=10 deps=[] autonomy=auto obstacle="" -->

### Файловый манифест
- `scripts/circle_plan.py` — CLI (символ `main`).
- `tests/test_plan.py` — тесты.

Текст фазы.

## Фаза 2 - Вторая
<!-- circle: status=pending order=20 deps=[1] autonomy=auto obstacle="" -->

Без манифеста.

## Журнал

- Фаза 1: сделала X, verify зелёный.
"""


class TestIdent(unittest.TestCase):
    def test_stable_with_salt(self):
        a = tele.ident("my-plan", "s3cr3t")
        self.assertEqual(a, tele.ident("my-plan", "s3cr3t"))
        self.assertRegex(a, r"\A[0-9a-f]{16}\Z")

    def test_salt_changes_digest(self):
        self.assertNotEqual(tele.ident("p", "salt-A"), tele.ident("p", "salt-B"))

    def test_no_salt_is_anon(self):
        self.assertEqual(tele.ident("p", None), "anon")
        self.assertEqual(tele.ident("p", ""), "anon")

    def test_irreversible_without_salt(self):
        self.assertNotEqual(tele.ident("host1", "salt"), hashlib.sha256(b"host1").hexdigest()[:16])


class TestGate(unittest.TestCase):
    def test_check_enum(self):
        self.assertEqual(tele.check_enum("done", tele.OUTCOMES), "done")
        self.assertIsNone(tele.check_enum("rm -rf /", tele.OUTCOMES))

    def test_clamp_int(self):
        self.assertEqual(tele.clamp_int(50, 0, 99), 50)
        self.assertEqual(tele.clamp_int(999, 0, 99), 99)
        self.assertEqual(tele.clamp_int(-5, 0, 99), 0)
        self.assertEqual(tele.clamp_int("nan", 0, 99), 0)

    def test_string_ok(self):
        self.assertTrue(tele.string_ok("a1b2c3d4e5f60718"))
        self.assertTrue(tele.string_ok("1.2.3"))
        self.assertTrue(tele.string_ok("anon"))
        self.assertFalse(tele.string_ok("src/app/secret.py"))
        self.assertFalse(tele.string_ok("user@host.com"))
        self.assertFalse(tele.string_ok("token = ABCDEF"))
        self.assertFalse(tele.string_ok('"quoted"'))


class TestParsing(unittest.TestCase):
    def test_has_codebase_map(self):
        self.assertTrue(tele.has_codebase_map(_PLAN))
        self.assertFalse(tele.has_codebase_map("# План\n## Фаза 1 — X\n"))


class TestRecordPhase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()

    def test_files_changed_counts_not_paths(self):
        rec = tele.record_phase(
            self.tmp, _PLAN, "1",
            ordinal=10, attempts=1, duration_s=42, outcome="done",
            plan_changed=True, committed=True, deps_count=0, autonomy="auto",
            subphases_added=0,
            touched_paths=["scripts/circle_plan.py", "scripts/new_blind.py"],
        )
        self.assertEqual(rec["files_changed"], 2)
        self.assertNotIn("new_blind.py", json.dumps(rec))  # путь НЕ в записи
        # мёртвые манифест-поля удалены из схемы v2 (всегда были 0 — покрытие не считалось)
        for k in ("manifest_declared", "files_off_manifest",
                  "manifest_declared_untouched", "coverage_ratio"):
            self.assertNotIn(k, rec)

    def test_bad_outcome_enum_dropped(self):
        rec = tele.record_phase(
            self.tmp, _PLAN, "1", ordinal=10, attempts=1, duration_s=1,
            outcome="rm -rf /", plan_changed=False, committed=False,
            deps_count=0, autonomy="auto", subphases_added=0, touched_paths=[],
        )
        self.assertIsNone(rec.get("outcome"))

    def test_context_pct_recorded_and_clamped(self):
        rec = tele.record_phase(
            self.tmp, _PLAN, "1", ordinal=10, attempts=1, duration_s=1,
            outcome="done", plan_changed=True, committed=True,
            deps_count=0, autonomy="auto", subphases_added=0, touched_paths=[],
            context_pct="37",
        )
        self.assertEqual(rec["context_pct"], 37)
        # вне диапазона зажимается, мусор/пусто → None (честное «неизвестно»)
        self.assertEqual(
            tele.record_phase(self.tmp, _PLAN, "1", ordinal=10, attempts=1, duration_s=1,
                              outcome="done", plan_changed=True, committed=True, deps_count=0,
                              autonomy="auto", subphases_added=0, touched_paths=[],
                              context_pct="250")["context_pct"], 100)
        for bad in ("", "?", "abc", None):
            r = tele.record_phase(self.tmp, _PLAN, "1", ordinal=10, attempts=1, duration_s=1,
                                  outcome="done", plan_changed=True, committed=True, deps_count=0,
                                  autonomy="auto", subphases_added=0, touched_paths=[],
                                  context_pct=bad)
            self.assertIsNone(r["context_pct"], bad)

    def test_context_pct_defaults_none_when_omitted(self):
        rec = tele.record_phase(
            self.tmp, _PLAN, "1", ordinal=10, attempts=1, duration_s=1,
            outcome="done", plan_changed=True, committed=True,
            deps_count=0, autonomy="auto", subphases_added=0, touched_paths=[],
        )
        self.assertIsNone(rec["context_pct"])

    def _miss(self, token):
        return tele.record_phase(
            self.tmp, _PLAN, "1", ordinal=10, attempts=1, duration_s=1,
            outcome="done", plan_changed=True, committed=True, deps_count=0,
            autonomy="auto", subphases_added=0, touched_paths=[], manifest_misses=token,
        )["manifest_miss_count"]

    def test_manifest_miss_token_valid(self):
        self.assertEqual(self._miss("miss(3)"), 3)
        self.assertEqual(self._miss("ok"), 0)          # отчитанный ноль — 0, НЕ None
        self.assertEqual(self._miss("miss(0)"), 0)
        # регистронезависимо + обрамляющие пробелы/перевод строки (файл пишется printf'ом)
        self.assertEqual(self._miss("OK"), 0)
        self.assertEqual(self._miss("Ok\n"), 0)
        self.assertEqual(self._miss("MISS(2)"), 2)
        self.assertEqual(self._miss("  miss(2)\n"), 2)
        self.assertEqual(self._miss("miss(999999)"), 100000)  # зажим сверху

    def test_manifest_miss_token_garbage_is_none(self):
        # None = «не отчиталась», отличимо от отчитанного 0. Строгий разбор: проза/голое число/
        # хвост после токена НЕ склеиваются в фейк-цифру, а честно дают None.
        for bad in (None, "", "?", "abc", "3", "0",
                    "miss(2) — 2 renamed files", "miss(2)miss(3)", "miss()",
                    "miss(-5)", "miss(2)(3)", "misses(2)"):
            self.assertIsNone(self._miss(bad), bad)

    def test_journal_bytes_positive(self):
        rec = tele.record_phase(
            self.tmp, _PLAN, "1", ordinal=10, attempts=1, duration_s=1,
            outcome="done", plan_changed=True, committed=True,
            deps_count=0, autonomy="auto", subphases_added=0, touched_paths=[],
        )
        self.assertGreater(rec["journal_digest_bytes"], 0)

    def test_appended_to_staging(self):
        for _ in range(2):
            tele.record_phase(
                self.tmp, _PLAN, "1", ordinal=10, attempts=1, duration_s=1,
                outcome="done", plan_changed=True, committed=True,
                deps_count=0, autonomy="auto", subphases_added=0, touched_paths=[],
            )
        with open(os.path.join(self.tmp, "run-stats", "phases.jsonl")) as f:
            self.assertEqual(len(f.read().strip().splitlines()), 2)


class TestScrubAndBuild(unittest.TestCase):
    def test_scrub_passes_clean(self):
        self.assertTrue(tele.scrub_record(
            {"plan_id": "a1b2c3d4e5f60718", "outcome": "done", "n": 5,
             "phases": [{"o": "blocked"}]}
        ))

    def test_scrub_fails_on_leak(self):
        self.assertFalse(tele.scrub_record({"leak": "src/app/secret.py"}))
        self.assertFalse(tele.scrub_record({"phases": [{"path": "user@host"}]}))

    def test_build_ok(self):
        rec = tele.build_run_record(
            plan_text=_PLAN, plugin_version="1.2.3", machine="host", plan_slug="p",
            salt="s", stop_reason="complete", run_wall_s=100, sessions_total=2,
            phases_total=2, status_counts={"done": 2},
            phase_recs=[{"ordinal": 1, "outcome": "done"}],
        )
        self.assertEqual(rec["schema_version"], tele.SCHEMA_VERSION)
        self.assertEqual(rec["stop_reason"], "complete")
        self.assertRegex(rec["plan_id"], r"\A[0-9a-f]{16}\Z")
        self.assertRegex(rec["run_uuid"], r"\A[0-9a-f]{12}\Z")
        self.assertTrue(rec["has_codebase_map"])
        self.assertEqual(rec["status_counts"], {"done": 2})

    def test_build_run_uuid_override(self):
        # Валидный переданный run_uuid используется как есть (склейка снимков одного прогона на
        # приёмнике); мусорный/инъекционный — игнорируется, генерится свежий.
        rec = tele.build_run_record(
            plan_text=_PLAN, plugin_version="1.2.3", machine="host", plan_slug="p",
            salt="s", stop_reason="complete", run_wall_s=1, sessions_total=1,
            phases_total=1, status_counts={}, phase_recs=[], run_uuid="abcdef012345",
        )
        self.assertEqual(rec["run_uuid"], "abcdef012345")
        bad = tele.build_run_record(
            plan_text=_PLAN, plugin_version="1.2.3", machine="host", plan_slug="p",
            salt="s", stop_reason="complete", run_wall_s=1, sessions_total=1,
            phases_total=1, status_counts={}, phase_recs=[], run_uuid="../evil",
        )
        self.assertRegex(bad["run_uuid"], r"\A[0-9a-f]{12}\Z")
        self.assertNotEqual(bad["run_uuid"], "../evil")

    def test_build_fail_closed_on_leaked_phase(self):
        rec = tele.build_run_record(
            plan_text=_PLAN, plugin_version="1.2.3", machine="host", plan_slug="p",
            salt="s", stop_reason="complete", run_wall_s=100, sessions_total=2,
            phases_total=2, status_counts={"done": 2},
            phase_recs=[{"ordinal": 1, "outcome": "done", "leaked": "/etc/passwd"}],
        )
        self.assertIsNone(rec)

    def test_build_drops_bad_stop_reason(self):
        rec = tele.build_run_record(
            plan_text=_PLAN, plugin_version="1.2.3", machine="host", plan_slug="p",
            salt="s", stop_reason="evil; rm", run_wall_s=1, sessions_total=1,
            phases_total=1, status_counts={}, phase_recs=[],
        )
        self.assertIsNone(rec)


class TestCliBuildRun(unittest.TestCase):
    def _run(self, args, env_extra=None):
        import subprocess
        import sys
        env = {**os.environ, **(env_extra or {})}
        return subprocess.run(
            [sys.executable, str(_SCRIPT), *args],
            capture_output=True, text=True, env=env,
        )

    def test_record_then_build_writes_one_file(self):
        tmp = tempfile.mkdtemp()
        plan = os.path.join(tmp, "myplan.md")
        with open(plan, "w") as f:
            f.write(_PLAN)
        work = os.path.join(tmp, "work")
        os.makedirs(work)
        # record-phase через CLI: touched paths из stdin
        import subprocess
        import sys
        r = subprocess.run(
            [sys.executable, str(_SCRIPT), "record-phase", plan, "1",
             "--work", work, "--ordinal", "10", "--attempts", "1",
             "--duration-s", "5", "--outcome", "done", "--plan-changed", "1",
             "--committed", "1", "--deps-count", "0", "--autonomy", "auto",
             "--subphases-added", "0"],
            input="scripts/circle_plan.py\n", capture_output=True, text=True,
            env={**os.environ, "CIRCLE_TELEMETRY_SALT": "s"},
        )
        self.assertEqual(r.returncode, 0, r.stderr)
        out_dir = os.path.join(tmp, "runs")
        os.makedirs(out_dir)
        r = self._run(
            ["build-run", plan, "--work", work, "--out-dir", out_dir,
             "--plugin-version", "1.2.3", "--stop-reason", "complete",
             "--run-wall-s", "10", "--sessions-total", "1", "--phases-total", "2",
             "--status-counts", "done=1,pending=1"],
            {"CIRCLE_TELEMETRY_SALT": "s"},
        )
        self.assertEqual(r.returncode, 0, r.stderr)
        files = os.listdir(out_dir)
        self.assertEqual(len(files), 1)
        name = files[0]
        # имя = <machine_id>-<plan_id>-<uuid>.json, все компоненты опаковые
        self.assertRegex(name, r"\A[0-9a-f]{16}-[0-9a-f]{16}-[0-9a-f]{12}\.json\Z")
        self.assertEqual(name, r.stdout.strip())  # build-run печатает имя в stdout
        with open(os.path.join(out_dir, name)) as f:
            data = json.load(f)
        self.assertEqual(len(data["phases"]), 1)
        self.assertEqual(data["phases"][0]["files_changed"], 1)
        self.assertEqual(data["status_counts"], {"done": 1, "pending": 1})
        self.assertNotIn("circle_plan.py", json.dumps(data))  # ни одного пути

    def test_build_run_fail_closed_writes_nothing(self):
        tmp = tempfile.mkdtemp()
        plan = os.path.join(tmp, "p.md")
        with open(plan, "w") as f:
            f.write(_PLAN)
        work = os.path.join(tmp, "work")
        os.makedirs(work)
        out_dir = os.path.join(tmp, "runs")
        os.makedirs(out_dir)
        r = self._run(
            ["build-run", plan, "--work", work, "--out-dir", out_dir,
             "--stop-reason", "evil; rm"],  # плохой обязательный enum → дроп
            {"CIRCLE_TELEMETRY_SALT": "s"},
        )
        self.assertEqual(r.returncode, 0)  # best-effort: не валимся
        self.assertIn("DROPPED", r.stderr)
        self.assertEqual(os.listdir(out_dir), [])


if __name__ == "__main__":
    unittest.main()
