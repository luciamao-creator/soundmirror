#!/usr/bin/env bash
# 一键复核 Agent Core v0
#
# 这个脚本是给你用的,不是给我用的。
# 它会重跑所有验证、重新生成产物、打印关键数字。
# 跑完之后你可以自己看 out/ 目录里的原始数据。
set -euo pipefail

cd "$(dirname "$0")/../.."          # → agent/
export PYTHONPATH="$PWD"

echo "═══════════════════════════════════════════════════════════"
echo "  SoundMirror Agent Core v0 — 独立复核"
echo "  $(date '+%Y-%m-%d %H:%M:%S')"
echo "═══════════════════════════════════════════════════════════"

# 1) 环境自检
echo
echo "[1/4] 环境"
python3 -c "import sys; print(f'  Python {sys.version.split()[0]}')"
for b in ffmpeg ffprobe; do
  if command -v "$b" >/dev/null 2>&1; then
    echo "  ✓ $b: $(command -v $b)"
  else
    echo "  ✗ $b 缺失 —— 音频产物校验会退化为'未验证'"
  fi
done
if python3 -m pytest --version >/dev/null 2>&1; then
  echo "  ✓ pytest: $(python3 -m pytest --version 2>&1 | head -1)"
else
  echo "  ✗ pytest 缺失 —— 回归测试会标为'未验证'"
  echo "      安装: python3 -m pip install --break-system-packages pytest"
fi

# 2) 跑证据采集
echo
echo "[2/4] 采集证据(约 90 秒)"
rm -f soundmirror/evidence/V0_EVIDENCE.json
python3 soundmirror/evidence/collect_v0_evidence.py
RC=$?

# 3) 独立复核产物(不信任采集器的结论,重新算一遍)
echo
echo "[3/4] 独立复核(重新计算 sha256 / 时长 / DB 状态)"
python3 soundmirror/evidence/independent_recheck.py

# 4) 产物清单
echo
echo "[4/4] 产物清单"
echo "  主证据:   soundmirror/evidence/V0_EVIDENCE.json"
echo "  原始数据: soundmirror/evidence/artifacts/"
ls -la soundmirror/evidence/artifacts/ | tail -n +2 | awk '{printf "    %8s  %s\n", $5, $9}'
echo
echo "  音频产物:"
python3 - <<'PY'
import json, os
d = json.load(open("soundmirror/evidence/V0_EVIDENCE.json", encoding="utf-8"))
for m in d.get("E2_artifacts", {}).get("manifest", []):
    p = m.get("path") or ""
    if p and os.path.exists(p):
        print(f"    {os.path.getsize(p):>7d}B  {m.get('duration_sec')}s  {p}")
PY

echo
echo "═══════════════════════════════════════════════════════════"
if [ $RC -eq 0 ]; then
  echo "  结论: 全部可验证断言通过"
else
  echo "  结论: 存在失败断言,详见 V0_EVIDENCE.json 的 claims[]"
fi
echo "═══════════════════════════════════════════════════════════"
exit $RC
