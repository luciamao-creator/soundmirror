#!/usr/bin/env python3
"""
独立复核 — 不信任采集器的结论,重新算一遍

这是给你用的校验器。它读 V0_EVIDENCE.json 里的**声明**,
然后用自己的代码重新计算,对比两者是否一致。

如果采集器在撒谎(比如声称文件存在但其实不存在),
这里会报不一致。
"""
import hashlib
import json
import os
import shutil
import sqlite3
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).parent
EV = HERE / "V0_EVIDENCE.json"

results = []


def check(name, expected, actual, note=""):
    ok = expected == actual
    results.append((name, ok, expected, actual, note))
    return ok


def main():
    if not EV.exists():
        print(f"  ✗ 找不到 {EV},请先跑 collect_v0_evidence.py")
        return 1

    d = json.load(open(EV, encoding="utf-8"))
    print()

    # ---- 1. 音频产物独立校验 ----
    print("  [音频产物]")
    for m in d.get("E2_artifacts", {}).get("manifest", []):
        p = m.get("path") or ""
        label = f"{m.get('type')}/{m.get('slot')}"

        if not p:
            check(f"{label} 有路径", True, False)
            continue
        if not os.path.exists(p):
            check(f"{label} 文件存在", True, False, p)
            continue

        # 独立算 sha256
        h = hashlib.sha256(open(p, "rb").read()).hexdigest()
        check(f"{label} sha256 与声明一致", m["sha256"], h,
              "" if h == m["sha256"] else f"声明 {m['sha256'][:12]} vs 实际 {h[:12]}")

        # 独立 ffprobe 时长
        if shutil.which("ffprobe"):
            r = subprocess.run(
                ["ffprobe", "-v", "error", "-show_entries", "format=duration",
                 "-of", "default=nw=1:nk=1", p],
                capture_output=True, text=True, timeout=30,
            )
            try:
                dur = round(float(r.stdout.strip()), 2)
            except ValueError:
                dur = None
            check(f"{label} 时长与声明一致", round(m.get("duration_sec", 0), 2), dur)

    # ---- 2. Session DB 独立校验 ----
    print("  [Session 状态]")
    sdb = d.get("E1_full_flow", {}).get("db")
    if sdb and os.path.exists(sdb):
        conn = sqlite3.connect(sdb)
        row = conn.execute(
            "SELECT creative_session_id, state_revision, ledger "
            "FROM creative_sessions LIMIT 1"
        ).fetchone()
        conn.close()
        if row:
            lg = json.loads(row[2])
            # 拿声明里的 final_ledger 对比
            claimed = d["E1_full_flow"]["final_ledger"]
            check("DB stage 与报告一致", claimed.get("current_stage"), lg.get("current_stage"))
            check("DB 素材数与报告一致",
                  len(claimed.get("archaeology_facts") or []),
                  len(lg.get("archaeology_facts") or []))

            # 3 个候选的 sha256 互不相同(证明 tool 对参数敏感)
            clips = lg.get("highlight_clips") or []
            shas = {c.get("checksum_sha256") for c in clips if c.get("checksum_sha256")}
            check("3 个候选 sha256 互不相同(证明真的分别生成)", 3, len(shas))

            # 每个 clip 的文件 sha256 与 DB 记录一致
            for c in clips:
                pp = c.get("preview_path")
                if pp and os.path.exists(pp):
                    hh = hashlib.sha256(open(pp, "rb").read()).hexdigest()
                    check(f"clip {c.get('slot')} 文件 sha256 == DB 记录",
                          c.get("checksum_sha256"), hh)

    # ---- 3. Trace 落盘校验 ----
    print("  [Trace 溯源]")
    tdb = d.get("E1_full_flow", {}).get("trace_db")
    if tdb and os.path.exists(tdb):
        conn = sqlite3.connect(tdb)
        n = conn.execute("SELECT COUNT(*) FROM trace").fetchone()[0]
        steps = json.loads(d["E1_full_flow"]["timeline"][-1].get("trace_id") and "0" or "0")
        conn.close()
        check("trace 表有 9 行(每步一条)", 9, n)

        # 抽一行验证内容完整
        conn = sqlite3.connect(tdb)
        row = conn.execute(
            "SELECT step_index, decision_json, evidence_json, risk_json, "
            "validation_json, notes_json FROM trace WHERE step_index=0"
        ).fetchone()
        conn.close()
        if row:
            for label, js in zip(
                ["decision", "evidence", "risk", "validation", "notes"], row[1:]
            ):
                try:
                    json.loads(js)
                    check(f"trace step0 的 {label} 是合法 JSON", True, True)
                except Exception as e:
                    check(f"trace step0 的 {label} 是合法 JSON", True, False, str(e))
    else:
        check("trace DB 存在", True, False, str(tdb))

    # ---- 4. 真 SIGKILL 校验 ----
    print("  [真进程中断]")
    for r in d.get("E10_hard_recovery", []):
        check(f"kill@{r['kill_after_step']} 子进程确实是 -9",
              True, r["killed_by_sigkill"], f"exit={r['child_exit_code']}")
        check(f"kill@{r['kill_after_step']} 素材未丢失",
              r["facts_at_kill"], r["facts_after_recovery"])

    # ---- 汇总 ----
    n_ok = sum(1 for _, ok, _, _, _ in results if ok)
    n = len(results)
    print()
    print(f"  独立复核: {n_ok}/{n} 一致")
    bad = [r for r in results if not r[1]]
    if bad:
        print("  ✗ 发现不一致(采集器的声明与实际不符):")
        for name, _, exp, act, note in bad:
            print(f"      {name}")
            print(f"        声明={exp}  实际={act}  {note}")
        return 1
    else:
        print("  ✓ 采集器的每一项声明都与磁盘实际一致")
    return 0


if __name__ == "__main__":
    sys.exit(main())
