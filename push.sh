#!/usr/bin/env bash
# 一键推送到 GitHub
# 用法:
#   1. 在 GitHub 上创建空仓库(不要选 README/gitignore/license)
#   2. 编辑下面的变量
#   3. bash push.sh

set -euo pipefail

# === 配置 ===
GITHUB_USER="${GITHUB_USER:-在这里填你的GitHub用户名}"
GITHUB_REPO="${GITHUB_REPO:-soundmirror}"
GITHUB_PAT="${GITHUB_PAT:-在这里填你的Personal Access Token}"
BRANCH="${BRANCH:-master}"

# === 检查 ===
if [[ "$GITHUB_USER" == *"这里填"* ]]; then
  echo "✗ 先编辑 push.sh,填入 GITHUB_USER / GITHUB_PAT / GITHUB_REPO"
  exit 1
fi

if [[ "$GITHUB_PAT" == *"这里填"* ]]; then
  echo "✗ 先编辑 push.sh,填入 GITHUB_PAT"
  exit 1
fi

# === 检查依赖 ===
if ! command -v git >/dev/null; then
  echo "✗ 找不到 git,请先安装"; exit 1
fi

# === 检查仓库 ===
if [[ ! -d .git ]]; then
  echo "✗ 当前目录不是 git 仓库。请 cd 到 soundmirror-export 后再跑此脚本。"
  exit 1
fi

# === 推送 ===
echo "→ 推送 $GITHUB_USER/$GITHUB_REPO 分支 $BRANCH"
git remote remove origin 2>/dev/null || true
git remote add origin "https://${GITHUB_USER}:${GITHUB_PAT}@github.com/${GITHUB_USER}/${GITHUB_REPO}.git"

echo "→ 先 dry-run fetch 验证权限"
if ! git fetch --dry-run origin 2>&1; then
  echo "✗ 拉取失败,可能是 PAT 没 'repo' scope 或仓库名错"
  exit 1
fi

echo "→ push"
git push -u origin "$BRANCH" --force-with-lease

echo ""
echo "✓ 完成"
echo "  https://github.com/$GITHUB_USER/$GITHUB_REPO"

# === 清理 PAT(避免留在 config 里) ===
git remote remove origin 2>/dev/null || true
git remote add origin "https://github.com/${GITHUB_USER}/${GITHUB_REPO}.git"

echo ""
echo "✓ remote 已清掉,下次 push 需要重新填。"