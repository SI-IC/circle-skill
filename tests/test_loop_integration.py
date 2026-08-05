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

    def _git_plan(self):
        # план внутри git-репозитория с настроенной identity и начальным коммитом
        d = tempfile.mkdtemp()
        for args in (
            ["init", "-q"],
            ["config", "user.email", "t@t"],
            ["config", "user.name", "t"],
        ):
            subprocess.run(["git", "-C", d, *args], check=True)
        p = os.path.join(d, "plan.md")
        open(p, "w", encoding="utf-8").write(PLAN)
        subprocess.run(["git", "-C", d, "add", "plan.md"], check=True)
        subprocess.run(["git", "-C", d, "commit", "-q", "-m", "init"], check=True)
        return p

    def _git_plan_no_identity(self):
        # git-репозиторий БЕЗ настроенной identity (ни локальной, ни — через env в run_loop —
        # глобальной/системной). Начальный коммит делаем с временной identity через -c,
        # чтобы завести HEAD, но в конфиге репозитория identity не остаётся.
        d = tempfile.mkdtemp()
        subprocess.run(["git", "-C", d, "init", "-q"], check=True)
        p = os.path.join(d, "plan.md")
        open(p, "w", encoding="utf-8").write(PLAN)
        subprocess.run(["git", "-C", d, "add", "plan.md"], check=True)
        subprocess.run(
            ["git", "-C", d, "-c", "user.email=seed@seed", "-c", "user.name=seed",
             "commit", "-q", "-m", "init"],
            check=True,
        )
        return p

    def _no_identity_env(self):
        # Нейтрализуем глобальную/системную git-identity для подпроцесса цикла,
        # чтобы воспроизвести реальный кейс «Author identity unknown».
        return {"GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_SYSTEM": "/dev/null",
                "GIT_CONFIG_NOSYSTEM": "1", "HOME": tempfile.mkdtemp()}

    def _git_log(self, plan):
        return subprocess.run(
            ["git", "-C", os.path.dirname(plan), "log", "--oneline"],
            capture_output=True,
            text=True,
        ).stdout

    def _last_author_email(self, plan):
        return subprocess.run(
            ["git", "-C", os.path.dirname(plan), "log", "-1", "--format=%ae"],
            capture_output=True,
            text=True,
        ).stdout.strip()

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

    def test_passes_model_to_claude_when_set(self):
        plan = self._plan()
        argv_out = plan + ".argv"
        run_loop(plan, "done", extra_env={
            "CIRCLE_CLAUDE_MODEL": "claude-opus-5",
            "FAKE_ARGV_OUT": argv_out,
        })
        argv = open(argv_out, encoding="utf-8").read().split("\n")
        self.assertIn("--model", argv)
        self.assertEqual(argv[argv.index("--model") + 1], "claude-opus-5")

    def test_no_model_flag_when_unset(self):
        plan = self._plan()
        argv_out = plan + ".argv"
        run_loop(plan, "done", extra_env={"FAKE_ARGV_OUT": argv_out})
        argv = open(argv_out, encoding="utf-8").read().split("\n")
        self.assertNotIn("--model", argv)

    def test_rejects_malformed_model_before_first_phase(self):
        plan = self._plan()
        argv_out = plan + ".argv"
        r = run_loop(plan, "done", extra_env={
            "CIRCLE_CLAUDE_MODEL": '$(touch pwned)',
            "FAKE_ARGV_OUT": argv_out,
        })
        self.assertFalse(os.path.exists(argv_out), "claude не должен запускаться с недопустимой моделью")
        log = open(os.path.join(self._work(plan), "loop.log"), encoding="utf-8").read()
        self.assertIn("недопустимый формат", log)

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

    def test_stops_on_idle_timeout_before_wall(self):
        # Молчащая сессия обрывается по idle-дедлайну раньше абсолютного потолка:
        # idle=2s срабатывает задолго до wall=60s. Проверяет, что цикл прокидывает
        # --idle-timeout в run_phase и что причина видна в loop.log.
        plan = self._plan()
        run_loop(plan, "hang", timeout="60", extra_env={"CIRCLE_IDLE_TIMEOUT": "2"})
        self.assertIn("STOP_REASON=hang", self._summary(plan))
        log = open(os.path.join(self._work(plan), "loop.log"), encoding="utf-8").read()
        self.assertIn("CIRCLE_PHASE_END: idle", log)

    def test_stops_when_stuck_on_same_phase(self):
        plan = self._plan()
        run_loop(plan, "churn", extra_env={"CIRCLE_MAX_SAME_PHASE": "2"})
        s = self._summary(plan)
        self.assertIn("STOP_REASON=stuck", s)

    def test_session_commits_each_successful_phase(self):
        # Коммитит сама сессия фазы; цикл-сторож видит чистое дерево и пропускает дальше.
        plan = self._git_plan()
        run_loop(plan, "done")
        log = self._git_log(plan)
        self.assertIn("circle: phase 1", log)
        self.assertIn("circle: phase 2", log)

    def test_no_commit_env_disables_commit(self):
        # CIRCLE_NO_COMMIT=1 → COMMIT_ENABLED=no: сессии сказано не коммитить, guard выключен,
        # план исполняется до конца без коммитов circle (проект коммитит сам).
        plan = self._git_plan()
        run_loop(plan, "done", extra_env={"CIRCLE_NO_COMMIT": "1"})
        log = self._git_log(plan)
        self.assertNotIn("circle: phase", log)
        self.assertIn("STOP_REASON=complete", self._summary(plan))

    def test_blocked_phase_is_not_committed(self):
        # фаза не завершилась done → сессия ничего не коммитит, guard не трогает не-done фазу
        plan = self._git_plan()
        run_loop(plan, "blocked")
        log = self._git_log(plan)
        self.assertNotIn("circle: phase", log)
        # сквозная проводка S1: фаза 1 blocked ⇒ остаток не развязан ⇒ stop_reason=stalled (не complete)
        self.assertIn("STOP_REASON=stalled", self._summary(plan))

    def test_failing_hook_is_not_bypassed(self):
        # Хук проекта уважается: pre-commit, падающий на коммите сессии, НЕ обходится (--no-verify
        # убран). Незакоммиченную done-фазу guard возвращает на повтор; непочиняемый хук (fake не
        # умеет чинить) упирается в backstop MAX_SAME → stuck. Тихой потери/обхода работы нет.
        plan = self._git_plan()
        hooks = os.path.join(os.path.dirname(plan), ".git", "hooks")
        os.makedirs(hooks, exist_ok=True)
        hook = os.path.join(hooks, "pre-commit")
        with open(hook, "w") as f:
            f.write("#!/bin/sh\necho 'lint failed' >&2\nexit 1\n")
        os.chmod(hook, 0o755)
        run_loop(plan, "done", extra_env={"CIRCLE_MAX_SAME_PHASE": "2"})
        self.assertNotIn("circle: phase", self._git_log(plan))
        self.assertIn("STOP_REASON=stuck", self._summary(plan))

    def test_guard_bounces_done_left_uncommitted(self):
        # Инвариант «done ⇒ закоммичено»: сессия пометила done, но не закоммитила. Цикл-сторож
        # не теряет работу и не обходит — возвращает фазу на повтор; зацикливание ловит MAX_SAME.
        plan = self._git_plan()
        run_loop(plan, "done_dirty", extra_env={"CIRCLE_MAX_SAME_PHASE": "2"})
        self.assertNotIn("circle: phase", self._git_log(plan))
        self.assertIn("STOP_REASON=stuck", self._summary(plan))

    def test_commits_without_configured_identity(self):
        # Реальный кейс «Author identity unknown»: репозиторий без identity. Цикл экспортит
        # fallback-identity в окружение сессии — коммит проходит, работа не теряется.
        plan = self._git_plan_no_identity()
        run_loop(plan, "done", extra_env=self._no_identity_env())
        log = self._git_log(plan)
        self.assertIn("circle: phase 1", log)
        self.assertIn("circle: phase 2", log)
        self.assertEqual(self._last_author_email(plan), "circle-skill@local")

    def test_configured_identity_not_overridden(self):
        # Fallback-identity ставится ТОЛЬКО при отсутствии настоящей — иначе коммиты фаз
        # ушли бы не тому автору. Есть identity t@t → цикл env не экспортит, автор сохраняется.
        plan = self._git_plan()  # identity t@t
        run_loop(plan, "done")
        self.assertEqual(self._last_author_email(plan), "t@t")

    def test_ambient_env_identity_not_overridden(self):
        # Identity может прийти окружением (GIT_AUTHOR_*), а не только из git config. Цикл считает
        # её настоящей и НЕ перекрывает fallback'ом — коммиты идут от переданного автора.
        plan = self._git_plan_no_identity()
        env = self._no_identity_env()
        env.update({
            "GIT_AUTHOR_NAME": "Bob", "GIT_AUTHOR_EMAIL": "bob@amb",
            "GIT_COMMITTER_NAME": "Bob", "GIT_COMMITTER_EMAIL": "bob@amb",
        })
        run_loop(plan, "done", extra_env=env)
        self.assertIn("circle: phase 1", self._git_log(plan))
        self.assertEqual(self._last_author_email(plan), "bob@amb")

    def test_hook_residue_does_not_false_bounce(self):
        # pre-commit хук-форматтер может оставить residue уже ПОСЛЕ успешного коммита — дерево грязное,
        # но коммит фазы есть. Guard смотрит на сдвиг HEAD, а не на чистоту дерева → не баунсит.
        plan = self._git_plan()
        hooks = os.path.join(os.path.dirname(plan), ".git", "hooks")
        os.makedirs(hooks, exist_ok=True)
        hook = os.path.join(hooks, "pre-commit")
        with open(hook, "w") as f:
            f.write("#!/bin/sh\necho residue >> residue.txt\nexit 0\n")
        os.chmod(hook, 0o755)
        run_loop(plan, "done")
        self.assertIn("circle: phase 1", self._git_log(plan))
        self.assertIn("STOP_REASON=complete", self._summary(plan))

    def test_partial_identity_preserves_configured_field(self):
        # Частичная identity: настроен email, но не name. Fallback заполняет ТОЛЬКО пробел (name),
        # реальный email НЕ перекрывается (env приоритетнее config — затёр бы его).
        d = tempfile.mkdtemp()
        subprocess.run(["git", "-C", d, "init", "-q"], check=True)
        subprocess.run(["git", "-C", d, "config", "user.email", "alice@corp"], check=True)
        p = os.path.join(d, "plan.md")
        open(p, "w", encoding="utf-8").write(PLAN)
        subprocess.run(["git", "-C", d, "add", "plan.md"], check=True)
        subprocess.run(
            ["git", "-C", d, "-c", "user.name=seed", "commit", "-q", "-m", "init"], check=True
        )
        run_loop(p, "done", extra_env=self._no_identity_env())
        self.assertIn("circle: phase 1", self._git_log(p))
        self.assertEqual(self._last_author_email(p), "alice@corp")


if __name__ == "__main__":
    unittest.main()
