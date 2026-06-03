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
