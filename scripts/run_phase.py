#!/usr/bin/env python3
"""Запускает команду (интерактивный claude) под PTY; ждёт появления файла-результата,
затем убивает процесс. Коды: 0=result появился, 2=таймаут, 3=процесс вышел без result."""

import argparse, os, select, signal, sys, time


def _terminate(pid):
    for sig in (signal.SIGTERM, signal.SIGKILL):
        try:
            os.kill(pid, sig)
        except ProcessLookupError:
            return
        for _ in range(20):
            try:
                wpid, _ = os.waitpid(pid, os.WNOHANG)
            except ChildProcessError:
                return
            if wpid == pid:
                return
            time.sleep(0.1)


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
    try:
        while True:
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
                rc = 0 if _result_ready(a.result) else 3
                break
            if time.monotonic() - start > a.timeout:
                rc = 2
                break
    finally:
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
