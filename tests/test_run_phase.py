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

    def test_idle_timeout_fires_on_silent_child(self):
        # Ребёнок один раз пишет в PTY, затем молчит. При включённом --idle-timeout
        # цикл обязан оборвать сессию по простою (rc=2) намного раньше абсолютного
        # --timeout, а не ждать весь потолок.
        cmd = [
            sys.executable,
            "-c",
            "import sys,time;sys.stdout.write('hi\\n');sys.stdout.flush();time.sleep(30)",
        ]
        t0 = time.monotonic()
        r = subprocess.run(
            [sys.executable, SCRIPT, "--result", self.result, "--timeout", "60",
             "--idle-timeout", "1", "--", *cmd],
            capture_output=True, text=True, timeout=60,
        )
        dt = time.monotonic() - t0
        self.assertEqual(r.returncode, 2, r.stderr)
        self.assertLess(dt, 15)  # оборвано по idle(~1s), не по wall(60s)

    def test_idle_timeout_not_tripped_by_active_child(self):
        # Ребёнок непрерывно пишет в PTY дольше idle-окна, затем пишет result.
        # Активность обязана сбрасывать таймер простоя → штатный rc=0, без ложного обрыва.
        cmd = [
            sys.executable,
            "-c",
            "import sys,time\n"
            "for _ in range(40):\n"
            "    sys.stdout.write('tick\\n'); sys.stdout.flush(); time.sleep(0.1)\n"
            f"open({self.result!r},'w').write('CIRCLE_RESULT: PHASE_DONE')\n",
        ]
        r = subprocess.run(
            [sys.executable, SCRIPT, "--result", self.result, "--timeout", "60",
             "--idle-timeout", "2", "--", *cmd],
            capture_output=True, text=True, timeout=60,
        )
        self.assertEqual(r.returncode, 0, r.stderr)  # 4s активности > 2s idle, но не оборвано

    def test_idle_timeout_disabled_by_default(self):
        # Без --idle-timeout (дефолт 0=выкл) молчание ребёнка НЕ обрывает сессию:
        # ребёнок молчит 2s и пишет result — обязан быть rc=0, а не idle-обрыв.
        cmd = [
            sys.executable,
            "-c",
            f"import time;time.sleep(2);open({self.result!r},'w').write('CIRCLE_RESULT: PHASE_DONE')",
        ]
        r = run(self.result, 10, cmd)
        self.assertEqual(r.returncode, 0, r.stderr)

    def test_rejects_non_finite_idle_timeout(self):
        cmd = [sys.executable, "-c", "pass"]
        r = subprocess.run(
            [sys.executable, SCRIPT, "--result", self.result, "--timeout", "5",
             "--idle-timeout", "nan", "--", *cmd],
            capture_output=True, text=True, timeout=30,
        )
        self.assertEqual(r.returncode, 4)

    def test_idle_timeout_logs_reason(self):
        # На обрыве по простою в --log должна лечь строка-маркер причины (пост-мортем).
        log = os.path.join(self.d, "loop.log")
        cmd = [
            sys.executable,
            "-c",
            "import sys,time;sys.stdout.write('hi\\n');sys.stdout.flush();time.sleep(30)",
        ]
        subprocess.run(
            [sys.executable, SCRIPT, "--result", self.result, "--timeout", "60",
             "--idle-timeout", "1", "--log", log, "--", *cmd],
            capture_output=True, text=True, timeout=60,
        )
        body = open(log, encoding="utf-8").read()
        self.assertIn("CIRCLE_PHASE_END: idle", body)

    def test_wall_timeout_logs_reason(self):
        # Обрыв по абсолютному потолку (idle выкл) кладёт в --log wall-маркер причины.
        log = os.path.join(self.d, "loop.log")
        cmd = [sys.executable, "-c", "import time;time.sleep(30)"]
        subprocess.run(
            [sys.executable, SCRIPT, "--result", self.result, "--timeout", "1",
             "--log", log, "--", *cmd],
            capture_output=True, text=True, timeout=60,
        )
        self.assertIn("CIRCLE_PHASE_END: wall-timeout", open(log, encoding="utf-8").read())

    def test_hard_deadline_logs_reason(self):
        # Жёсткий SIGALRM-путь (главный цикл заблокирован в select дольше timeout) тоже
        # обязан оставить wall-маркер — цикл документирует «причина — см. CIRCLE_PHASE_END».
        log = os.path.join(self.d, "loop.log")
        cmd = [sys.executable, "-c", "import time;time.sleep(20)"]
        subprocess.run(
            [sys.executable, SCRIPT, "--result", self.result, "--timeout", "1",
             "--poll", "20", "--log", log, "--", *cmd],
            capture_output=True, text=True, timeout=60,
        )
        self.assertIn("CIRCLE_PHASE_END: wall-timeout", open(log, encoding="utf-8").read())

    def test_end_marker_after_buffered_partial_frame(self):
        # Хронология пост-мортема: недотерминированный кадр (вывод без хвостового \n —
        # типичная TUI-перерисовка перед зависанием) обязан лечь в лог ДО маркера конца,
        # а не после. Ловит регресс, где _emit_end писал мимо ещё не сброшенного фильтра.
        log = os.path.join(self.d, "loop.log")
        cmd = [
            sys.executable,
            "-c",
            "import sys,time;sys.stdout.write('THINKING_NO_NL');sys.stdout.flush();time.sleep(30)",
        ]
        subprocess.run(
            [sys.executable, SCRIPT, "--result", self.result, "--timeout", "60",
             "--idle-timeout", "1", "--log", log, "--", *cmd],
            capture_output=True, text=True, timeout=60,
        )
        body = open(log, encoding="utf-8").read()
        self.assertIn("THINKING_NO_NL", body)
        self.assertIn("CIRCLE_PHASE_END: idle", body)
        self.assertLess(
            body.index("THINKING_NO_NL"), body.index("CIRCLE_PHASE_END"),
            "буферизованный кадр должен предшествовать маркеру конца",
        )

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


class TestContextOut(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp()
        self.result = os.path.join(self.d, "result")

    def test_context_out_writes_peak(self):
        # --context-out получает ПИК контекста за сессию (48), не последнее значение (20).
        ctxf = os.path.join(self.d, "context-pct")
        log = os.path.join(self.d, "loop.log")
        child = [
            sys.executable, "-c",
            "import sys,time\n"
            "for v in (12,48,20):\n"
            "    sys.stdout.write('bar %d%% | t\\n'%v); sys.stdout.flush(); time.sleep(0.1)\n"
            f"open({self.result!r},'w').write('CIRCLE_RESULT: PHASE_DONE')\n",
        ]
        subprocess.run(
            [sys.executable, SCRIPT, "--result", self.result, "--timeout", "10",
             "--log", log, "--context-out", ctxf, "--", *child],
            capture_output=True, text=True, timeout=60,
        )
        self.assertEqual(open(ctxf, encoding="utf-8").read().strip(), "48")

    def test_context_out_empty_when_no_statusbar(self):
        # Нет распознанного статус-бара → пустой файл (честное «неизвестно», не 0).
        ctxf = os.path.join(self.d, "context-pct")
        log = os.path.join(self.d, "loop.log")
        child = [
            sys.executable, "-c",
            f"import sys;sys.stdout.write('no bar\\n');sys.stdout.flush();"
            f"open({self.result!r},'w').write('CIRCLE_RESULT: PHASE_DONE')",
        ]
        subprocess.run(
            [sys.executable, SCRIPT, "--result", self.result, "--timeout", "10",
             "--log", log, "--context-out", ctxf, "--", *child],
            capture_output=True, text=True, timeout=60,
        )
        self.assertEqual(open(ctxf, encoding="utf-8").read().strip(), "")

    def test_context_diag_logged_when_bar_unparsed(self):
        # Бар-подобный вывод, где «N% |» не распознан → в лог кладём CIRCLE_CTX_UNPARSED
        # (локально, НЕ в телеметрию), чтобы регэксп чинить по реальной строке, а не гадать.
        ctxf = os.path.join(self.d, "context-pct")
        log = os.path.join(self.d, "loop.log")
        child = [
            sys.executable, "-c",
            "import sys\n"
            "sys.stdout.write('[m] bar 42% x | main\\n'); sys.stdout.flush()\n"
            f"open({self.result!r},'w').write('CIRCLE_RESULT: PHASE_DONE')\n",
        ]
        subprocess.run(
            [sys.executable, SCRIPT, "--result", self.result, "--timeout", "10",
             "--log", log, "--context-out", ctxf, "--", *child],
            capture_output=True, text=True, timeout=60,
        )
        self.assertEqual(open(ctxf, encoding="utf-8").read().strip(), "")
        self.assertIn("CIRCLE_CTX_UNPARSED", open(log, encoding="utf-8").read())

    def test_context_diag_written_to_dedicated_file(self):
        # --ctx-unparsed-out получает образцы нераспознанного бара в ВЫДЕЛЕННЫЙ файл (копится за
        # прогон, тривиально грепается — не тонет в многомегабайтном loop.log и доживает до разбора).
        # Родительский каталог run-stats ещё не существует — run_phase создаёт его best-effort.
        ctxf = os.path.join(self.d, "context-pct")
        log = os.path.join(self.d, "loop.log")
        unp = os.path.join(self.d, "run-stats", "ctx-unparsed.log")
        child = [
            sys.executable, "-c",
            "import sys\n"
            "sys.stdout.write('[m] bar 42% x | main\\n'); sys.stdout.flush()\n"
            f"open({self.result!r},'w').write('CIRCLE_RESULT: PHASE_DONE')\n",
        ]
        subprocess.run(
            [sys.executable, SCRIPT, "--result", self.result, "--timeout", "10",
             "--log", log, "--context-out", ctxf, "--ctx-unparsed-out", unp, "--", *child],
            capture_output=True, text=True, timeout=60,
        )
        self.assertIn("CIRCLE_CTX_UNPARSED", open(unp, encoding="utf-8").read())
        # при заданном --ctx-unparsed-out образец идёт туда, НЕ в loop.log (дублировать незачем)
        self.assertNotIn("CIRCLE_CTX_UNPARSED", open(log, encoding="utf-8").read())


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

    def test_context_peak_holds_max_not_last(self):
        # Пик держит максимум за сессию, даже если контекст потом «просел» (auto-compact
        # роняет %). Именно пик отвечает на «была ли фаза пухлой».
        f = self._filter(b"bar 12% | t\n", b"bar 48% | t\n", b"bar 15% | t\n")
        self.assertEqual(f.ctx, 15)  # текущий — последний
        self.assertEqual(f.ctx_peak, 48)  # пик — максимум

    def test_context_peak_none_when_never_seen(self):
        f = self._filter(b"no statusbar here\n")
        self.assertIsNone(f.ctx_peak)

    def test_context_diag_captures_drifted_barlike_frame(self):
        # Бар-подобный кадр («%» и «|» в одном кадре), но «N% |» не сматчился — вероятный
        # дрейф формата статус-бара: копим образец для локальной диагностики, ctx не выставлен.
        f = self._filter("[Opus] ░░░ 42% ⏱ | main\n".encode())
        self.assertIsNone(f.ctx_peak)
        self.assertEqual(len(f.ctx_diag), 1)
        self.assertIn(b"42%", f.ctx_diag[0])

    def test_context_diag_empty_when_bar_parses(self):
        # Распознанный бар → диагностику не копим (чинить нечего).
        f = self._filter(b"bar 42% | t\n")
        self.assertEqual(f.ctx_peak, 42)
        self.assertEqual(f.ctx_diag, [])

    def test_context_diag_ignores_plain_percent_text(self):
        # «%» в тексте без «|» — не кандидат в статус-бар, диагностику не засоряет.
        f = self._filter(b"progress 50% complete\n")
        self.assertEqual(f.ctx_diag, [])

    def test_context_diag_bounded_and_deduped(self):
        # Копим не больше _CTX_DIAG_MAX разных кадров; подряд-дубли и так гаснут раньше.
        f = self._filter(*[("v%d%% x | b\n" % i).encode() for i in range(20)])
        self.assertIsNone(f.ctx_peak)
        self.assertLessEqual(len(f.ctx_diag), rp._LogFilter._CTX_DIAG_MAX)


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
