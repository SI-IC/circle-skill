#!/usr/bin/env python3
"""Поддельный claude для интеграционного теста цикла. НЕ ходит в сеть."""

import os, re, sys, time


def find_prompt_path(argv):
    for a in argv:
        m = re.search(r"(\S+executor-prompt\.md)", a)
        if m:
            return m.group(1)
    return None


def repo_of(plan):
    # git-репозиторий, содержащий план (там же коммитит сессия)
    return os.popen(
        f"git -C {os.path.dirname(plan)} rev-parse --show-toplevel 2>/dev/null"
    ).read().strip()


def commit_work(plan, phase):
    # Имитируем добросовестную сессию: коммитит свою работу последним шагом.
    # Identity наследуется из окружения (цикл экспортит fallback, если её нет) или из конфига репо.
    # Хук проекта может отклонить коммит (rc != 0) — тогда дерево остаётся грязным и цикл-сторож
    # вернёт фазу на повтор; здесь best-effort, как у реальной сессии до починки причины.
    repo = repo_of(plan)
    if not repo:
        return
    os.system(f"git -C {repo} add -A")
    os.system(f'git -C {repo} commit -q -m "circle: phase {phase} — fake"')


def main():
    mode = os.environ.get("FAKE_MODE", "done")
    # Эмулируем нижний статус-бар TUI с потреблением контекста — run_phase вытащит из него
    # пик и положит в --context-out (сквозная проверка телеметрии context_pct).
    sys.stdout.write("[fake-model] ######## 40% | timer main\n")
    sys.stdout.flush()
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
    m = re.search(r"режим коммита:\s*\*\*`(\w+)`\*\*", text)
    commit_enabled = bool(m) and m.group(1) == "yes"

    if mode == "done":
        # добросовестная сессия: закрыла фазу, дописала журнал и закоммитила свою работу сама
        os.system(f"{sys.executable} {plan_cli} set-status {plan} {phase} done")
        with open(plan, "a", encoding="utf-8") as f:
            f.write(f"\n### fake: фаза {phase} выполнена\n")
        if commit_enabled:
            commit_work(plan, phase)
    elif mode == "done_dirty":
        # сессия ослушалась: пометила done и наследила, но НЕ закоммитила (даже в режиме yes).
        # Цикл-сторож обязан вернуть фазу в in_progress (баунс), а не потерять работу/обойти.
        os.system(f"{sys.executable} {plan_cli} set-status {plan} {phase} done")
        with open(plan, "a", encoding="utf-8") as f:
            f.write(f"\n### fake: фаза {phase} done без коммита\n")
    elif mode == "blocked":
        # фаза не завершилась: статус blocked, но план изменён (хеш другой, RC=0) —
        # guard трогает только done, blocked не коммитится (реальная сессия откатила бы работу).
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
