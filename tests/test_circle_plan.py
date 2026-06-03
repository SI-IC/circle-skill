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

    def test_skips_needs_human_and_skipped(self):
        phases = [
            self._ph(id="1", autonomy="needs-human", order=10),
            self._ph(id="2", status="skipped", order=20),
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


if __name__ == "__main__":
    unittest.main()
