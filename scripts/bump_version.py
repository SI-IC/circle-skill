#!/usr/bin/env python3
"""Синхронный bump семвер-версии в .claude-plugin/plugin.json и marketplace.json.

Использование:
  bump_version.py --current            # печатает текущую версию (или ошибку рассинхрона)
  bump_version.py patch|minor|major    # поднимает версию в обоих манифестах, печатает новую
  bump_version.py <level> --repo PATH  # для другого корня репозитория (по умолч. .)
"""

import argparse
import json
import os
import sys

PLUGIN = ".claude-plugin/plugin.json"
MARKET = ".claude-plugin/marketplace.json"
PLUGIN_NAME = "circle-skill"


def _parse(v):
    parts = v.split(".")
    if len(parts) != 3 or not all(p.isdigit() for p in parts):
        raise ValueError(f"некорректная семвер-версия: {v}")
    return [int(p) for p in parts]


def bump(v, level):
    major, minor, patch = _parse(v)
    if level == "major":
        return f"{major + 1}.0.0"
    if level == "minor":
        return f"{major}.{minor + 1}.0"
    if level == "patch":
        return f"{major}.{minor}.{patch + 1}"
    raise ValueError(f"неизвестный уровень bump: {level} (нужно patch|minor|major)")


def _load(repo):
    with open(os.path.join(repo, PLUGIN), encoding="utf-8") as f:
        pj = json.load(f)
    with open(os.path.join(repo, MARKET), encoding="utf-8") as f:
        mj = json.load(f)
    return pj, mj


def _market_entry(mj):
    for p in mj.get("plugins", []):
        if p.get("name") == PLUGIN_NAME:
            return p
    raise ValueError(f"в marketplace.json нет плагина {PLUGIN_NAME}")


def current_version(repo):
    pj, mj = _load(repo)
    pv = pj["version"]
    mv = _market_entry(mj)["version"]
    if pv != mv:
        raise ValueError(f"рассинхрон версий: plugin.json={pv} marketplace.json={mv}")
    return pv


def set_version(repo, newv):
    _parse(newv)  # валидация формата
    pj, mj = _load(repo)
    pj["version"] = newv
    _market_entry(mj)["version"] = newv
    with open(os.path.join(repo, PLUGIN), "w", encoding="utf-8") as f:
        json.dump(pj, f, ensure_ascii=False, indent=2)
        f.write("\n")
    with open(os.path.join(repo, MARKET), "w", encoding="utf-8") as f:
        json.dump(mj, f, ensure_ascii=False, indent=2)
        f.write("\n")


def main(argv=None):
    ap = argparse.ArgumentParser(prog="bump_version")
    ap.add_argument("level", nargs="?", choices=["patch", "minor", "major"])
    ap.add_argument("--repo", default=".")
    ap.add_argument("--current", action="store_true")
    a = ap.parse_args(argv)
    try:
        cur = current_version(a.repo)
        if a.current:
            print(cur)
            return 0
        if not a.level:
            print("укажи уровень: patch|minor|major (или --current)", file=sys.stderr)
            return 2
        newv = bump(cur, a.level)
        set_version(a.repo, newv)
        print(newv)
        return 0
    except (FileNotFoundError, KeyError, ValueError) as e:
        print(f"bump_version: ошибка: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
