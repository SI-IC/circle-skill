#!/usr/bin/env python3
"""Запускает команду (интерактивный claude) под PTY; ждёт появления файла-результата,
затем убивает процесс. Коды: 0=result появился, 2=таймаут, 3=процесс вышел без result."""

import argparse, math, os, select, signal, sys, time

# Запас поверх --timeout для жёсткого SIGALRM-дедлайна: страхует от блокировки в
# любом syscall главного цикла (select/read/waitpid), которую мягкая проверка не ловит.
_GRACE = 8


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


def main(argv=None):
    import pty

    ap = argparse.ArgumentParser()
    ap.add_argument("--result", required=True)
    ap.add_argument("--timeout", type=float, default=3600.0)
    ap.add_argument("--log", default=None)
    ap.add_argument("--poll", type=float, default=1.0)
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
    start = time.monotonic()
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
                if data and logf:
                    logf.write(data)
                    logf.flush()
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
        if logf:
            logf.close()
        try:
            os.close(fd)
        except OSError:
            pass
    return rc


if __name__ == "__main__":
    sys.exit(main())
