import io, os, sys, subprocess, tempfile, time, unittest

SCRIPT = os.path.join(os.path.dirname(__file__), "..", "scripts", "run_phase.py")
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))
import run_phase as rp


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


class TestLogFilter(unittest.TestCase):
    def _run(self, *chunks):
        buf = io.BytesIO()
        f = rp._LogFilter(buf)
        for c in chunks:
            f.feed(c)
        f.flush()
        return buf.getvalue()

    def test_strips_ansi_colors(self):
        out = self._run(b"\x1b[31mhello\x1b[0m\n")
        self.assertEqual(out, b"hello\n")

    def test_carriage_return_is_frame_boundary(self):
        # Каждый отличающийся кадр сохраняется (нарратив не теряем), не только последний.
        out = self._run(b"1%\r2%\r3%\n")
        self.assertEqual(out, b"1%\n2%\n3%\n")

    def test_crlf_does_not_emit_empty_frame(self):
        out = self._run(b"TICK\r\n")
        self.assertEqual(out, b"TICK\n")  # \r\n → один кадр, пустой хвост выкинут

    def test_collapses_consecutive_duplicate_frames(self):
        out = self._run(b"TICK\nTICK\nTICK\n")
        self.assertEqual(out, b"TICK\n")  # подряд идущие дубли схлопнуты

    def test_collapses_repeated_spinner_frames(self):
        out = self._run(b"working\rworking\rworking\rdone\n")
        self.assertEqual(out, b"working\ndone\n")  # анимация спиннера → один кадр

    def test_keeps_non_consecutive_repeats(self):
        out = self._run(b"a\nb\na\n")
        self.assertEqual(out, b"a\nb\na\n")  # дедуп только соседних кадров

    def test_drops_empty_and_whitespace_frames(self):
        out = self._run(b"\x1b[2K\n   \n\thi\n")
        self.assertEqual(
            out, b"\thi\n"
        )  # пустые/пробельные кадры выкинуты, \t сохранён

    def test_buffers_partial_line_until_newline(self):
        out = self._run(b"par", b"tial line\n")
        self.assertEqual(out, b"partial line\n")

    def test_flush_emits_trailing_line_without_newline(self):
        out = self._run(b"no newline at end")
        self.assertEqual(out, b"no newline at end\n")

    def test_oversized_frame_without_terminator_is_flushed_not_lost(self):
        # Страховка _MAX_FRAME: очень длинный кадр без \r/\n не копится бесконечно,
        # данные не теряются, буфер опустошается.
        big = b"A" * (rp._LogFilter._MAX_FRAME + 10)
        buf = io.BytesIO()
        f = rp._LogFilter(buf)
        f.feed(big)
        self.assertEqual(f.buf, b"")  # буфер сброшен страховкой
        self.assertIn(b"A" * 1000, buf.getvalue())  # данные не потеряны

    def test_strips_osc_title_sequence(self):
        out = self._run(b"\x1b]0;window title\x07prompt\n")
        self.assertEqual(out, b"prompt\n")

    def _filter(self, *chunks):
        buf = io.BytesIO()
        f = rp._LogFilter(buf)
        for c in chunks:
            f.feed(c)
        return f

    def test_extracts_context_pct_from_statusline(self):
        # реальный кадр нижнего статус-бара TUI (снят с живой сессии claude)
        f = self._filter(
            "[Opus 4.8 (1M context)] ░░░░░░░░░░ 42% | ⏱ 3m | main ~6\n".encode()
        )
        self.assertEqual(f.ctx, 42)

    def test_plain_percent_without_pipe_is_not_context(self):
        # `%` в тексте сообщения (без `|`-разделителя статус-бара) не считаем контекстом
        f = self._filter(b"progress 50% complete\n")
        self.assertIsNone(f.ctx)

    def test_context_pct_updates_to_latest(self):
        f = self._filter(b"bar 10% | t\n", b"bar 25% | t\n")
        self.assertEqual(f.ctx, 25)


class TestProgressLine(unittest.TestCase):
    def test_with_ctx_and_label(self):
        self.assertEqual(
            rp._progress_line(12, "фаза 2"), "CIRCLE_PROGRESS: контекст 12% · фаза 2"
        )

    def test_unknown_ctx_shows_question_mark(self):
        self.assertEqual(
            rp._progress_line(None, "фаза 2"), "CIRCLE_PROGRESS: контекст ? · фаза 2"
        )

    def test_no_label(self):
        self.assertEqual(rp._progress_line(50, ""), "CIRCLE_PROGRESS: контекст 50%")


class TestHeartbeat(unittest.TestCase):
    def test_heartbeat_written_to_log_with_context(self):
        d = tempfile.mkdtemp()
        result = os.path.join(d, "result")
        log = os.path.join(d, "loop.log")
        # дочерний процесс печатает статус-бар с контекстом и держится (result не пишет)
        child = [
            sys.executable,
            "-c",
            "import sys,time;sys.stdout.write('bar 37% | timer\\n');sys.stdout.flush();time.sleep(1.5)",
        ]
        subprocess.run(
            [sys.executable, SCRIPT, "--result", result, "--timeout", "5",
             "--log", log, "--heartbeat", "0.3", "--label", "фаза 1", "--", *child],
            capture_output=True, text=True, timeout=60,
        )
        body = open(log, encoding="utf-8").read()
        self.assertIn("CIRCLE_PROGRESS: контекст 37% · фаза 1", body)


if __name__ == "__main__":
    unittest.main()
