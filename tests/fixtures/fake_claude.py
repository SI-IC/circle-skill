#!/usr/bin/env python3
"""Поддельный claude для интеграционного теста цикла. НЕ ходит в сеть."""

import os, re, sys, time


def find_prompt_path(argv):
    for a in argv:
        m = re.search(r"(\S+executor-prompt\.md)", a)
        if m:
            return m.group(1)
    return None


def main():
    mode = os.environ.get("FAKE_MODE", "done")
    if mode == "hang":
        time.sleep(300)
        return 0
    prompt_path = find_prompt_path(sys.argv)
    if not prompt_path or not os.path.exists(prompt_path):
        return 1
    text = open(prompt_path, encoding="utf-8").read()
    plan = re.search(r"План:\s*`([^`]+)`", text).group(1)
    phase = re.search(r"Назначенная фаза:\s*\*\*([^*]+)\*\*", text).group(1)
    work = re.search(r"Рабочая папка плагина:\s*`([^`]+)`", text).group(1)
    plan_cli = re.search(r"CLI статусов:\s*`python3 ([^`]+)`", text).group(1)

    if mode == "done":
        os.system(f"{sys.executable} {plan_cli} set-status {plan} {phase} done")
        with open(plan, "a", encoding="utf-8") as f:
            f.write(f"\n### fake: фаза {phase} выполнена\n")
    elif mode == "blocked":
        # фаза не завершилась: статус blocked, но план изменён (хеш другой, RC=0) —
        # цикл дойдёт до гейта коммита и должен пропустить (коммитим только done).
        os.system(
            f"{sys.executable} {plan_cli} set-status {plan} {phase} blocked --obstacle test"
        )
        with open(plan, "a", encoding="utf-8") as f:
            f.write(f"\n### fake: фаза {phase} заблокирована\n")
    elif mode == "churn":
        os.system(f"{sys.executable} {plan_cli} set-status {plan} {phase} in_progress")
        with open(plan, "a", encoding="utf-8") as f:
            f.write(f"\n### churn {phase} {os.urandom(4).hex()}\n")
    # mode == "nothing": план не трогаем (эмуляция зависшей-без-прогресса сессии)

    # сигнал циклу
    with open(os.path.join(work, "result"), "w", encoding="utf-8") as f:
        f.write("CIRCLE_RESULT: PHASE_DONE\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
