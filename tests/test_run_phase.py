import os, sys, subprocess, tempfile, time, unittest

SCRIPT = os.path.join(os.path.dirname(__file__), "..", "scripts", "run_phase.py")


def run(result, timeout, cmd):
    return subprocess.run(
        [
            sys.executable,
            SCRIPT,
            "--result",
            result,
            "--timeout",
            str(timeout),
            "--",
            *cmd,
        ],
        capture_output=True,
        text=True,
        timeout=60,
    )


class TestRunPhase(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp()
        self.result = os.path.join(self.d, "result")

    def test_returns_0_when_result_appears(self):
        cmd = [
            sys.executable,
            "-c",
            f"import time;time.sleep(0.3);open({self.result!r},'w').write('CIRCLE_RESULT: PHASE_DONE')",
        ]
        r = run(self.result, 10, cmd)
        self.assertEqual(r.returncode, 0)
        self.assertTrue(os.path.exists(self.result))

    def test_returns_2_on_timeout_and_kills(self):
        cmd = [sys.executable, "-c", "import time;time.sleep(30)"]
        t0 = time.monotonic()
        r = run(self.result, 1, cmd)
        self.assertEqual(r.returncode, 2)
        self.assertLess(time.monotonic() - t0, 15)

    def test_hard_deadline_fires_when_blocked_past_timeout(self):
        # Жёсткий watchdog: если главный цикл заблокирован в syscall (здесь — select
        # на большом --poll при молчаливом живом ребёнке) дольше --timeout, run_phase
        # ВСЁ РАВНО обязан вернуть rc=2 за ~timeout+grace, а не висеть ~poll секунд.
        # Это тот же класс бага, что висящий os.waitpid (стек подтвердил блок в __wait4).
        cmd = [sys.executable, "-c", "import time;time.sleep(20)"]
        t0 = time.monotonic()
        r = subprocess.run(
            [
                sys.executable,
                SCRIPT,
                "--result",
                self.result,
                "--timeout",
                "1",
                "--poll",
                "20",
                "--",
                *cmd,
            ],
            capture_output=True,
            text=True,
            timeout=60,
        )
        dt = time.monotonic() - t0
        self.assertEqual(r.returncode, 2, r.stderr)
        self.assertLess(dt, 15)  # дедлайн timeout(1)+grace ≈ 9s, не ~20s poll

    def test_returns_2_when_child_ignores_sigterm(self):
        # Ребёнок игнорирует SIGTERM и непрерывно пишет в PTY. _terminate обязан
        # эскалировать до SIGKILL и завершиться за разумное время, не зависнув.
        src = (
            "import signal,sys,time\n"
            "signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
            "while True:\n"
            "    sys.stdout.write('x'); sys.stdout.flush(); time.sleep(0.01)\n"
        )
        cmd = [sys.executable, "-c", src]
        t0 = time.monotonic()
        r = run(self.result, 1, cmd)
        dt = time.monotonic() - t0
        self.assertEqual(r.returncode, 2, r.stderr)
        self.assertLess(dt, 15)

    def test_log_captures_pty_output_and_is_closed_on_timeout(self):
        # --log должен наполняться выводом ребёнка и корректно закрываться даже при rc=2.
        log = os.path.join(self.d, "loop.log")
        cmd = [
            sys.executable,
            "-u",
            "-c",
            "import time\nwhile True:\n print('TICK'); time.sleep(0.1)",
        ]
        r = subprocess.run(
            [
                sys.executable,
                SCRIPT,
                "--result",
                self.result,
                "--timeout",
                "1",
                "--log",
                log,
                "--",
                *cmd,
            ],
            capture_output=True,
            text=True,
            timeout=60,
        )
        self.assertEqual(r.returncode, 2, r.stderr)
        with open(log, "rb") as fh:
            self.assertIn(b"TICK", fh.read())

    def test_rejects_non_finite_timeout(self):
        cmd = [sys.executable, "-c", "pass"]
        r = subprocess.run(
            [
                sys.executable,
                SCRIPT,
                "--result",
                self.result,
                "--timeout",
                "nan",
                "--",
                *cmd,
            ],
            capture_output=True,
            text=True,
            timeout=30,
        )
        self.assertEqual(r.returncode, 4)

    def test_returns_3_when_child_exits_without_result(self):
        cmd = [sys.executable, "-c", "import sys;sys.exit(0)"]
        r = run(self.result, 10, cmd)
        self.assertEqual(r.returncode, 3)

    def test_returns_0_when_result_written_then_child_exits_fast(self):
        # Процесс пишет result и СРАЗУ выходит — не должно быть спурьёзного rc=3.
        cmd = [
            sys.executable,
            "-c",
            f"open({self.result!r},'w').write('CIRCLE_RESULT: PHASE_DONE')",
        ]
        r = run(self.result, 10, cmd)
        self.assertEqual(r.returncode, 0)


if __name__ == "__main__":
    unittest.main()
