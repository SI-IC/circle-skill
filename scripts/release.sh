#!/usr/bin/env bash
# Релиз circle-skill: поднять версию → коммит → push main → тег → push тега.
# Использование: ./scripts/release.sh [patch|minor|major]   (по умолчанию patch)
set -euo pipefail

HERE="$(cd "$(dirname "$0")/.." && pwd)"
cd "$HERE"
LEVEL="${1:-patch}"
PY="${CIRCLE_PYTHON:-python3}"
REMOTE="${CIRCLE_REMOTE:-origin}"
BRANCH="main"

case "$LEVEL" in patch|minor|major) ;; *) echo "release: уровень должен быть patch|minor|major" >&2; exit 2 ;; esac

if [ -n "$(git status --porcelain)" ]; then
  echo "release: рабочее дерево не чистое — закоммить или спрячь изменения сначала" >&2
  exit 1
fi
if [ "$(git rev-parse --abbrev-ref HEAD)" != "$BRANCH" ]; then
  echo "release: релиз делается с ветки $BRANCH (сейчас $(git rev-parse --abbrev-ref HEAD))" >&2
  exit 1
fi

NEWV="$("$PY" scripts/bump_version.py "$LEVEL")"
TAG="circle-skill--v$NEWV"
echo "release: версия → $NEWV ($TAG)"

git add .claude-plugin/plugin.json .claude-plugin/marketplace.json
git commit -m "chore(release): v$NEWV"

# Сначала пушим ветку (на этот момент тега $TAG ещё нет — pre-push видит cur > последний тег),
# затем создаём и пушим тег.
git push "$REMOTE" "$BRANCH"
git tag -a "$TAG" -m "circle-skill $NEWV"
git push "$REMOTE" "$TAG"
echo "release: готово — $NEWV запушен в $REMOTE ($BRANCH + $TAG)"
