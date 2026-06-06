import os, sys, unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))
import circle_plan as cp

PLAN = """# План: тест

## Фаза 1 — Логин
<!-- circle: status=done order=10 deps=[] autonomy=auto obstacle="" -->
Тело фазы 1.

## Фаза 2 — Перенос
<!-- circle: status=pending order=20 deps=[1] autonomy=auto obstacle="" -->
Тело фазы 2.

## Фаза 3 — Дроп прод-таблиц
<!-- circle: status=pending order=30 deps=[2] autonomy=needs-human obstacle="" -->
Тело фазы 3.
"""


class TestParse(unittest.TestCase):
    def test_parses_all_phases(self):
        ph = cp.parse_phases(PLAN)
        self.assertEqual([p.id for p in ph], ["1", "2", "3"])

    def test_parses_marker_fields(self):
        ph = {p.id: p for p in cp.parse_phases(PLAN)}
        self.assertEqual(ph["1"].status, "done")
        self.assertEqual(ph["2"].order, 20)
        self.assertEqual(ph["2"].deps, ["1"])
        self.assertEqual(ph["3"].autonomy, "needs-human")

    def test_title_parsed(self):
        ph = {p.id: p for p in cp.parse_phases(PLAN)}
        self.assertEqual(ph["1"].title, "Логин")

    def test_phase_without_marker_defaults_pending(self):
        text = "## Фаза 9 — Без маркера\nтело\n"
        ph = cp.parse_phases(text)
        self.assertEqual(ph[0].status, "pending")
        self.assertEqual(ph[0].marker_line, -1)

    def test_obstacle_with_spaces(self):
        text = (
            "## Фаза 5 — X\n"
            '<!-- circle: status=blocked order=1 deps=[] autonomy=auto obstacle="нет доступа к БД" -->\n'
        )
        ph = cp.parse_phases(text)[0]
        self.assertEqual(ph.obstacle, "нет доступа к БД")
        self.assertEqual(ph.status, "blocked")


class TestSelect(unittest.TestCase):
    def _ph(self, **kw):
        base = dict(
            id="x", title="t", status="pending", order=0, deps=[], autonomy="auto"
        )
        base.update(kw)
        return cp.Phase(**base)

    def test_picks_lowest_order_eligible_pending(self):
        phases = [self._ph(id="b", order=20), self._ph(id="a", order=10)]
        self.assertEqual(cp.select_next(phases).id, "a")

    def test_skips_phase_with_unmet_deps(self):
        phases = [self._ph(id="1", status="pending", order=10, deps=["0"])]
        self.assertIsNone(cp.select_next(phases))

    def test_deps_met_when_done(self):
        phases = [
            self._ph(id="1", status="done", order=10),
            self._ph(id="2", status="pending", order=20, deps=["1"]),
        ]
        self.assertEqual(cp.select_next(phases).id, "2")

    def test_skips_needs_human(self):
        phases = [self._ph(id="1", autonomy="needs-human", order=10)]
        self.assertIsNone(cp.select_next(phases))

    def test_skips_skipped_status(self):
        phases = [self._ph(id="2", status="skipped", order=20)]
        self.assertIsNone(cp.select_next(phases))

    def test_dep_on_skipped_keeps_dependent_unselected(self):
        phases = [
            self._ph(id="1", status="skipped", order=10),
            self._ph(id="2", status="pending", order=20, deps=["1"]),
        ]
        self.assertIsNone(cp.select_next(phases))

    def test_in_progress_takes_priority(self):
        phases = [
            self._ph(id="1", status="in_progress", order=99),
            self._ph(id="2", status="pending", order=1),
        ]
        self.assertEqual(cp.select_next(phases).id, "1")

    def test_is_complete_when_nothing_eligible(self):
        phases = [
            self._ph(id="1", status="done", order=10),
            self._ph(id="2", status="blocked", order=20),
            self._ph(id="3", status="skipped", order=30),
        ]
        self.assertTrue(cp.is_complete(phases))

    def test_not_complete_when_pending_eligible(self):
        phases = [self._ph(id="1", status="pending", order=10)]
        self.assertFalse(cp.is_complete(phases))


class TestWrite(unittest.TestCase):
    def test_set_status_preserves_other_fields(self):
        text = (
            "## Фаза 2 — X\n"
            '<!-- circle: status=pending order=20 deps=[1] autonomy=auto obstacle="" -->\n'
            "тело\n"
        )
        out = cp.set_status(text, "2", "done")
        p = {x.id: x for x in cp.parse_phases(out)}["2"]
        self.assertEqual(p.status, "done")
        self.assertEqual(p.order, 20)
        self.assertEqual(p.deps, ["1"])

    def test_set_status_writes_obstacle(self):
        text = (
            "## Фаза 2 — X\n"
            '<!-- circle: status=pending order=20 deps=[] autonomy=auto obstacle="" -->\n'
        )
        out = cp.set_status(text, "2", "blocked", obstacle='нет доступа "root"')
        p = cp.parse_phases(out)[0]
        self.assertEqual(p.status, "blocked")
        self.assertEqual(p.obstacle, 'нет доступа "root"')

    def test_set_status_missing_marker_raises(self):
        text = "## Фаза 2 — X\nтело\n"
        with self.assertRaises(ValueError):
            cp.set_status(text, "2", "done")

    def test_add_marker_inserts_under_heading(self):
        text = "## Фаза 7 — Новая\nтело\n"
        out = cp.add_marker(
            text, "7", status="pending", order=70, deps=["1"], autonomy="needs-human"
        )
        p = cp.parse_phases(out)[0]
        self.assertEqual(p.marker_line, 1)
        self.assertEqual(p.order, 70)
        self.assertEqual(p.autonomy, "needs-human")

    def test_add_marker_idempotent_overwrites(self):
        text = (
            "## Фаза 7 — Новая\n"
            '<!-- circle: status=pending order=1 deps=[] autonomy=auto obstacle="" -->\n'
        )
        out = cp.add_marker(text, "7", status="done", order=70)
        ph = cp.parse_phases(out)
        self.assertEqual(len(ph), 1)
        self.assertEqual(ph[0].status, "done")

    def test_set_status_preserves_crlf(self):
        text = (
            "## Фаза 2 — X\r\n"
            '<!-- circle: status=pending order=20 deps=[] autonomy=auto obstacle="" -->\r\n'
            "тело\r\n"
        )
        out = cp.set_status(text, "2", "done")
        self.assertIn("\r\n", out)
        self.assertNotIn("\n\n", out.replace("\r\n", ""))  # no stray bare LF introduced
        self.assertEqual(cp.parse_phases(out)[0].status, "done")


import json
import subprocess
import tempfile


class TestJournal(unittest.TestCase):
    PLAN = (
        "## Фаза 1 — A\n"
        '<!-- circle: status=done order=10 deps=[] autonomy=auto obstacle="" -->\n'
        "тело\n\n"
        "## Журнал\n\n"
        "### Фаза 1 — A — 2026-06-05\n"
        "- сделал X; verify зелёный.\n"
        "- Следующий шаг: Фаза 2.\n"
    )

    def test_extracts_journal_body(self):
        body = cp.journal_section(self.PLAN)
        self.assertIn("### Фаза 1 — A", body)
        self.assertIn("Следующий шаг: Фаза 2", body)

    def test_journal_excludes_heading_and_phase_bodies(self):
        body = cp.journal_section(self.PLAN)
        self.assertNotIn("## Журнал", body)  # сам заголовок секции не включаем
        self.assertNotIn("тело", body)  # тело фазы выше журнала не попадает

    def test_no_journal_returns_empty(self):
        text = "## Фаза 1 — A\nтело\n"
        self.assertEqual(cp.journal_section(text), "")

    def test_empty_input_returns_empty(self):
        self.assertEqual(cp.journal_section(""), "")

    def test_journal_stops_at_next_level2_heading(self):
        text = "## Журнал\n### Фаза 1 — A\nзапись.\n## Прочее\nне журнал.\n"
        body = cp.journal_section(text)
        self.assertIn("запись.", body)
        self.assertNotIn("не журнал.", body)


class TestPhaseSlice(unittest.TestCase):
    PLAN = (
        "# Заголовок плана\n\n"
        "Goal: построить X.\n\n"
        "## Conventions (read once)\n"
        "Общие правила, нужные каждой фазе.\n\n"
        "## Фаза 1 — A\n"
        '<!-- circle: status=done order=10 deps=[] autonomy=auto obstacle="" -->\n'
        "Тело фазы 1 — детали реализации.\n\n"
        "## Фаза 2 — B\n"
        '<!-- circle: status=pending order=20 deps=[1] autonomy=auto obstacle="" -->\n'
        "Тело фазы 2 — детали.\n\n"
        "## Журнал\n\n"
        "### Фаза 1 — A\n- сделал X; следующий шаг: Фаза 2.\n"
    )

    def test_includes_preamble_phase_body_and_journal(self):
        s = cp.phase_slice(self.PLAN, "2")
        self.assertIn("Goal: построить X.", s)  # преамбула
        self.assertIn("Общие правила", s)  # read-once Conventions
        self.assertIn("Тело фазы 2 — детали.", s)  # тело назначенной фазы
        self.assertIn("следующий шаг: Фаза 2", s)  # журнал

    def test_excludes_other_phase_bodies(self):
        s = cp.phase_slice(self.PLAN, "2")
        self.assertNotIn("Тело фазы 1 — детали", s)  # чужая фаза в срез не попадает

    def test_phase_heading_and_marker_present(self):
        s = cp.phase_slice(self.PLAN, "2")
        self.assertIn("## Фаза 2 — B", s)
        self.assertIn("status=pending", s)

    def test_no_journal_section_ok(self):
        text = (
            "Преамбула.\n\n"
            "## Фаза 1 — A\n"
            '<!-- circle: status=pending order=10 deps=[] autonomy=auto obstacle="" -->\n'
            "тело.\n"
        )
        s = cp.phase_slice(text, "1")
        self.assertIn("Преамбула.", s)
        self.assertIn("тело.", s)
        self.assertNotIn("Журнал предыдущих фаз", s)

    def test_missing_phase_raises(self):
        with self.assertRaises(KeyError):
            cp.phase_slice(self.PLAN, "99")

    def test_crlf_input_slice_is_parseable(self):
        # phase-context.md — одноразовый файл для чтения моделью, не пишется обратно в план,
        # поэтому байтовая идентичность CRLF не требуется; важно лишь что срез корректен.
        s = cp.phase_slice(self.PLAN.replace("\n", "\r\n"), "2")
        self.assertIn("Тело фазы 2", s)
        self.assertIn("Goal: построить X.", s)
        self.assertNotIn("Тело фазы 1 — детали", s)

    def test_cli_phase_slice(self):
        f = tempfile.NamedTemporaryFile(
            "w", suffix=".md", delete=False, encoding="utf-8"
        )
        f.write(self.PLAN)
        f.close()
        script = os.path.join(
            os.path.dirname(__file__), "..", "scripts", "circle_plan.py"
        )
        r = subprocess.run(
            [sys.executable, script, "phase-slice", f.name, "2"],
            capture_output=True,
            text=True,
        )
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("Тело фазы 2", r.stdout)
        self.assertIn("Goal: построить X.", r.stdout)
        self.assertNotIn("Тело фазы 1 — детали", r.stdout)

    def test_cli_phase_slice_missing_phase_returns_1(self):
        f = tempfile.NamedTemporaryFile(
            "w", suffix=".md", delete=False, encoding="utf-8"
        )
        f.write(self.PLAN)
        f.close()
        script = os.path.join(
            os.path.dirname(__file__), "..", "scripts", "circle_plan.py"
        )
        r = subprocess.run(
            [sys.executable, script, "phase-slice", f.name, "99"],
            capture_output=True,
            text=True,
        )
        self.assertEqual(r.returncode, 1)
        self.assertIn("ошибка", r.stderr)


class TestSummaryCLI(unittest.TestCase):
    PLAN = (
        "## Фаза 1 — A\n"
        '<!-- circle: status=done order=10 deps=[] autonomy=auto obstacle="" -->\n'
        "## Фаза 2 — B\n"
        '<!-- circle: status=blocked order=20 deps=[] autonomy=auto obstacle="нет БД" -->\n'
        "## Фаза 3 — C\n"
        '<!-- circle: status=pending order=30 deps=[1] autonomy=auto obstacle="" -->\n'
    )

    def test_summary_counts(self):
        s = cp.summary(cp.parse_phases(self.PLAN))
        self.assertEqual(s["counts"]["done"], 1)
        self.assertEqual(s["counts"]["blocked"], 1)
        self.assertEqual(s["blocked"][0]["obstacle"], "нет БД")

    def _write(self, text):
        f = tempfile.NamedTemporaryFile(
            "w", suffix=".md", delete=False, encoding="utf-8"
        )
        f.write(text)
        f.close()
        return f.name

    def _cli(self, *args):
        script = os.path.join(
            os.path.dirname(__file__), "..", "scripts", "circle_plan.py"
        )
        return subprocess.run(
            [sys.executable, script, *args], capture_output=True, text=True
        )

    def test_cli_next_prints_id_tab_title(self):
        path = self._write(self.PLAN)
        r = self._cli("next", path)
        self.assertEqual(r.returncode, 0)
        self.assertEqual(r.stdout.strip(), "3\tC")

    def test_cli_next_none_when_complete(self):
        path = self._write(
            self.PLAN.replace(
                "status=pending order=30 deps=[1]", "status=done order=30 deps=[1]"
            )
        )
        r = self._cli("next", path)
        self.assertEqual(r.stdout.strip(), "NONE")

    def test_cli_set_status_roundtrip(self):
        path = self._write(self.PLAN)
        r = self._cli("set-status", path, "3", "done")
        self.assertEqual(r.returncode, 0, r.stderr)
        with open(path, encoding="utf-8") as fh:
            p = {x.id: x for x in cp.parse_phases(fh.read())}
        self.assertEqual(p["3"].status, "done")

    def test_cli_phases_json(self):
        path = self._write(self.PLAN)
        r = self._cli("phases", path, "--json")
        data = json.loads(r.stdout)
        self.assertEqual(len(data), 3)
        self.assertEqual(data[0]["id"], "1")

    def test_cli_missing_file_returns_1(self):
        r = self._cli("next", "/nonexistent/plan-xyz.md")
        self.assertEqual(r.returncode, 1)
        self.assertIn("ошибка", r.stderr)

    def test_cli_journal_prints_body(self):
        path = self._write(
            self.PLAN
            + "## Журнал\n\n### Фаза 1 — A — 2026-06-05\n- готово; следующий шаг: Фаза 3.\n"
        )
        r = self._cli("journal", path)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("следующий шаг: Фаза 3", r.stdout)
        self.assertNotIn("## Журнал", r.stdout)

    def test_cli_journal_empty_when_absent(self):
        path = self._write(self.PLAN)  # без секции Журнал
        r = self._cli("journal", path)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout.strip(), "")


if __name__ == "__main__":
    unittest.main()
