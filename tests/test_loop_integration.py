import os, subprocess, sys, tempfile, unittest

ROOT = os.path.join(os.path.dirname(__file__), "..")
LOOP = os.path.join(ROOT, "scripts", "circle-loop.sh")
FAKE = os.path.join(ROOT, "tests", "fixtures", "fake_claude.py")

PLAN = (
    "# План тест\n\n"
    '## Фаза 1 — A\n<!-- circle: status=pending order=10 deps=[] autonomy=auto obstacle="" -->\n\n'
    '## Фаза 2 — B\n<!-- circle: status=pending order=20 deps=[1] autonomy=auto obstacle="" -->\n\n'
    '## Фаза 3 — C\n<!-- circle: status=pending order=30 deps=[] autonomy=needs-human obstacle="" -->\n\n'
    "## Журнал\n"
)


def run_loop(plan_path, fake_mode, timeout="5", extra_env=None):
    env = dict(os.environ)
    env["FAKE_MODE"] = fake_mode
    env["CIRCLE_TIMEOUT"] = timeout
    env["CLAUDE_PLUGIN_ROOT"] = ROOT
    # claude-bin = обёртка, перенаправляющая в fake_claude.py (она получит флаг и start-prompt)
    wrapper = plan_path + ".claude.sh"
    with open(wrapper, "w") as f:
        f.write(f'#!/usr/bin/env bash\nexec "{sys.executable}" "{FAKE}" "$@"\n')
    os.chmod(wrapper, 0o755)
    env["CIRCLE_CLAUDE_BIN"] = wrapper
    if extra_env:
        env.update(extra_env)
    return subprocess.run(
        ["bash", LOOP, plan_path], env=env, capture_output=True, text=True, timeout=120
    )


class TestLoopIntegration(unittest.TestCase):
    def _plan(self):
        d = tempfile.mkdtemp()
        p = os.path.join(d, "plan.md")
        open(p, "w", encoding="utf-8").write(PLAN)
        return p

    def _work(self, plan):
        # рабочая папка — отдельная на план: .circle/<имя-плана>/
        slug = os.path.splitext(os.path.basename(plan))[0]
        return os.path.join(os.path.dirname(plan), ".circle", slug)

    def _summary(self, plan):
        return open(
            os.path.join(self._work(plan), "summary.txt"), encoding="utf-8"
        ).read()

    def test_runs_to_completion_skipping_needs_human(self):
        plan = self._plan()
        run_loop(plan, "done")
        s = self._summary(plan)
        self.assertIn("STOP_REASON=complete", s)
        text = open(plan, encoding="utf-8").read()
        sys.path.insert(0, os.path.join(ROOT, "scripts"))
        import circle_plan as cp

        ph = {p.id: p for p in cp.parse_phases(text)}
        self.assertEqual(ph["1"].status, "done")
        self.assertEqual(ph["2"].status, "done")
        self.assertEqual(ph["3"].status, "pending")  # needs-human не тронута

    def test_generates_phase_context_and_gitignore(self):
        plan = self._plan()
        run_loop(plan, "done")
        # срез фазы лёг в рабочую папку
        ctx = os.path.join(self._work(plan), "phase-context.md")
        self.assertTrue(os.path.exists(ctx), "phase-context.md не создан")
        body = open(ctx, encoding="utf-8").read()
        self.assertIn("Фаза", body)
        # вся .circle/ гарантированно вне VCS
        gi = os.path.join(os.path.dirname(plan), ".circle", ".gitignore")
        self.assertEqual(open(gi, encoding="utf-8").read().strip(), "*")

    def test_separate_work_dirs_per_plan(self):
        # Два плана в одной директории не должны делить .circle/ (логи/result/summary).
        d = tempfile.mkdtemp()
        p1 = os.path.join(d, "alpha.md")
        p2 = os.path.join(d, "beta.md")
        open(p1, "w", encoding="utf-8").write(PLAN)
        open(p2, "w", encoding="utf-8").write(PLAN)
        run_loop(p1, "done")
        run_loop(p2, "done")
        self.assertTrue(os.path.exists(os.path.join(self._work(p1), "summary.txt")))
        self.assertTrue(os.path.exists(os.path.join(self._work(p2), "summary.txt")))
        self.assertNotEqual(self._work(p1), self._work(p2))

    def test_stops_on_no_progress(self):
        plan = self._plan()
        run_loop(plan, "nothing")
        s = self._summary(plan)
        self.assertIn("STOP_REASON=no-progress", s)

    def test_stops_on_hang_timeout(self):
        plan = self._plan()
        run_loop(plan, "hang", timeout="2")
        s = self._summary(plan)
        self.assertIn("STOP_REASON=hang", s)

    def test_stops_when_stuck_on_same_phase(self):
        plan = self._plan()
        run_loop(plan, "churn", extra_env={"CIRCLE_MAX_SAME_PHASE": "2"})
        s = self._summary(plan)
        self.assertIn("STOP_REASON=stuck", s)


if __name__ == "__main__":
    unittest.main()
