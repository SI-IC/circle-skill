#!/usr/bin/env python3
"""Запускает команду (интерактивный claude) под PTY; ждёт появления файла-результата,
затем убивает процесс. Коды: 0=result появился, 2=таймаут, 3=процесс вышел без result."""

import argparse, math, os, re, select, signal, sys, time

# Запас поверх --timeout для жёсткого SIGALRM-дедлайна: страхует от блокировки в
# любом syscall главного цикла (select/read/waitpid), которую мягкая проверка не ловит.
_GRACE = 8

# ANSI/управляющие коды для чистки PTY-вывода перед записью в лог.
_ANSI_RE = re.compile(
    rb"\x1b\[[0-9;?]*[ -/]*[@-~]"  # CSI: \e[ ... финальный байт (цвета, перемещение курсора)
    rb"|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)"  # OSC: \e] ... BEL|ST (заголовки окна)
    rb"|\x1b[PX^_][^\x1b]*\x1b\\"  # DCS/SOS/PM/APC ... ST
    rb"|\x1b[\x20-\x2f]*[\x30-\x7e]"  # двухсимвольные esc (\e(, \e= и пр.)
    rb"|[\x00-\x08\x0b-\x1f\x7f]"  # прочие control-байты (кроме \t=09 и \n=0a)
)


# Потребление контекста сессии из нижнего статус-бара TUI: «… N% | ⏱ …». Берём число перед
# `%|`-разделителем — это заякорено на статус-строку и не ловит случайный `%` в тексте сообщения.
_CTX_RE = re.compile(rb"(\d+)%\s*\|")


class _LogFilter:
    r"""Чистит сырой PTY-вывод перед записью в лог. Интерактивный claude рисует TUI и
    перерисовывает экран десятки раз/сек (спиннеры, прогресс), отчего сырой лог раздут
    в разы и почти не грепается (в реальном прогоне — 7.4 МБ при всего 38 `\n`: весь TUI
    идёт `\r`-перерисовками).

    Подход НЕ теряющий нарратив (тот же, по которому делается ручной анализ прогонов):
    и `\r`, и `\n` трактуются как граница кадра; каждый кадр чистится от ANSI/control-кодов;
    выкидываются только пустые и подряд идущие ОДИНАКОВЫЕ кадры (анимация спиннера). Все
    содержательно отличающиеся кадры сохраняются — лог остаётся пригодным для пост-мортема."""

    _MAX_FRAME = 1 << 20  # страховка: кадр без терминатора не должен расти бесконечно

    def __init__(self, fh):
        self.fh = fh
        self.buf = b""
        self.last = None
        self.ctx = None  # последнее замеченное потребление контекста, % (None — ещё не видели)

    def feed(self, data):
        self.buf += data
        parts = re.split(rb"[\r\n]", self.buf)
        self.buf = parts.pop()  # хвост без терминатора — ждём продолжения в след. чанке
        for frame in parts:
            self._emit(frame)
        if len(self.buf) > self._MAX_FRAME:  # очень длинный кадр без \r/\n — сбрасываем
            self._emit(self.buf)
            self.buf = b""

    def _emit(self, frame):
        clean = _ANSI_RE.sub(b"", frame).rstrip()
        if not clean or clean == self.last:  # пустые / подряд-дубли кадров — пропускаем
            return
        self.last = clean
        m = _CTX_RE.search(clean)  # обновляем потребление контекста из статус-бара
        if m:
            self.ctx = int(m.group(1))
        self.fh.write(clean + b"\n")
        self.fh.flush()

    def flush(self):
        if self.buf:
            self._emit(self.buf)
            self.buf = b""


def _reap_bounded(pid, attempts):
    """Ограниченное ожидание реапа ребёнка через WNOHANG. True — реапнут/исчез.
    НИКОГДА не блокируется навечно: ребёнок может застрять в exiting-состоянии
    (kernel `E`-state) и не реапаться — на этом и виснул прежний blocking waitpid."""
    for _ in range(attempts):
        try:
            wpid, _ = os.waitpid(pid, os.WNOHANG)
        except ChildProcessError:
            return True
        if wpid == pid:
            return True
        time.sleep(0.1)
    return False


def _terminate(pid):
    # pty.fork()-ребёнок — лидер своей сессии/группы (setsid). Бьём по всей ГРУППЕ
    # (killpg по РЕАЛЬНОМУ pgid через getpgid, не полагаясь на инвариант pgid==pid),
    # чтобы заодно прибить потомков claude, держащих PTY. Наружу OSError не пробрасываем:
    # _terminate зовётся из finally, где дальше идёт закрытие fd/лога — исключение тут
    # оставило бы их незакрытыми.
    try:
        pgid = os.getpgid(pid)
    except OSError:
        pgid = pid  # ребёнок уже исчез/зомби — реап ниже разрулит
    for sig in (signal.SIGTERM, signal.SIGKILL):
        try:
            os.killpg(pgid, sig)
        except ProcessLookupError:
            break  # группы уже нет — ребёнок мёртв; дожнём зомби ниже
        except OSError:
            # лидер уже зомби (macOS отдаёт EPERM на killpg) или иной сбой — бьём лидера
            try:
                os.kill(pid, sig)
            except OSError:
                break
        if _reap_bounded(pid, 20):  # ~2s на штатную смерть
            return
    _reap_bounded(pid, 30)  # ~3s добор после SIGKILL, затем сдаёмся (не виснем)


def _result_ready(path):
    try:
        return os.path.getsize(path) > 0
    except OSError:
        return False


def _progress_line(ctx, label):
    """Одна строка-heartbeat: потребление контекста сессии (+ метка фазы). ctx=None → '?'."""
    pct = f"{ctx}%" if ctx is not None else "?"
    tail = f" · {label}" if label else ""
    return f"CIRCLE_PROGRESS: контекст {pct}{tail}"


def _emit_progress(fh, ctx, label):
    fh.write(
        (time.strftime("%Y-%m-%dT%H:%M:%S ") + _progress_line(ctx, label) + "\n").encode()
    )
    fh.flush()


def main(argv=None):
    import pty

    ap = argparse.ArgumentParser()
    ap.add_argument("--result", required=True)
    ap.add_argument("--timeout", type=float, default=3600.0)
    ap.add_argument("--log", default=None)
    ap.add_argument("--poll", type=float, default=1.0)
    ap.add_argument("--heartbeat", type=float, default=5.0)  # период строки прогресса, сек
    ap.add_argument("--label", default="")  # метка фазы для heartbeat
    ap.add_argument("cmd", nargs=argparse.REMAINDER)
    a = ap.parse_args(argv)
    cmd = a.cmd[1:] if a.cmd and a.cmd[0] == "--" else a.cmd
    if not cmd:
        print("run_phase: пустая команда", file=sys.stderr)
        return 4
    # Валидируем ДО форка: inf/nan timeout уронили бы signal.alarm (OverflowError/
    # ValueError) мимо finally — утекли бы дочерний процесс и SIGALRM-хендлер.
    if not (math.isfinite(a.timeout) and a.timeout > 0):
        print(
            "run_phase: timeout должен быть конечным положительным числом",
            file=sys.stderr,
        )
        return 4
    if not (math.isfinite(a.poll) and a.poll > 0):
        print(
            "run_phase: poll должен быть конечным положительным числом", file=sys.stderr
        )
        return 4

    try:
        os.unlink(a.result)
    except FileNotFoundError:
        pass

    pid, fd = pty.fork()
    if pid == 0:  # child
        try:
            os.execvp(cmd[0], cmd)
        except Exception as e:
            sys.stderr.write(f"exec failed: {e}\n")
        os._exit(127)

    logf = open(a.log, "ab") if a.log else None
    logfilter = _LogFilter(logf) if logf else None
    start = time.monotonic()
    next_hb = start + a.heartbeat  # когда писать следующий heartbeat прогресса
    rc = 0
    child_alive = True

    class _Deadline(Exception):
        pass

    def _on_alarm(_signum, _frame):
        raise _Deadline

    old_handler = signal.signal(signal.SIGALRM, _on_alarm)
    # Жёсткий backstop: SIGALRM прервёт ЛЮБОЙ заблокированный syscall главного цикла
    # (select/read/waitpid) и поднимет _Deadline, даже если мягкая проверка таймаута
    # ниже до него не доходит. Без него blocking-waitpid висел дольше таймаута.
    signal.alarm(int(math.ceil(a.timeout)) + _GRACE)
    try:
        while True:
            # PTY-буфер дренируем всегда (os.read ниже), иначе «болтливый» дочерний процесс
            # (реальный claude) заблокируется на записи в полный буфер. В лог пишем только при --log.
            try:
                r, _, _ = select.select([fd], [], [], a.poll)
            except (OSError, ValueError):
                r = []
            if r:
                try:
                    data = os.read(fd, 65536)
                except OSError:
                    data = b""
                if data and logfilter:
                    logfilter.feed(data)
            # Heartbeat прогресса: периодически пишем в лог потребление контекста сессии — под
            # строкой цикла «выбрана фаза X» видно, что сессия жива и сколько контекста съела.
            if logf and time.monotonic() >= next_hb:
                _emit_progress(logf, logfilter.ctx, a.label)
                next_hb = time.monotonic() + a.heartbeat
            if _result_ready(a.result):
                rc = 0
                break
            try:
                wpid, _ = os.waitpid(pid, os.WNOHANG)
            except ChildProcessError:
                wpid = pid
            if wpid == pid:
                child_alive = False
                # Грейс: процесс мог записать result и сразу выйти — дать файлу появиться/наполниться.
                rc = 3
                for _ in range(10):
                    if _result_ready(a.result):
                        rc = 0
                        break
                    time.sleep(0.05)
                break
            if time.monotonic() - start > a.timeout:
                rc = 2
                break
    except _Deadline:
        # Если result успел появиться до срабатывания дедлайна (гонка в окне между
        # удачным break и снятием alarm) — честно отдаём 0, не ложный таймаут.
        rc = 0 if _result_ready(a.result) else 2
    finally:
        signal.alarm(0)
        signal.signal(signal.SIGALRM, old_handler)
        if child_alive:
            _terminate(pid)
        if logfilter:
            logfilter.flush()
        if logf:
            logf.close()
        try:
            os.close(fd)
        except OSError:
            pass
    return rc


if __name__ == "__main__":
    sys.exit(main())
