#!/usr/bin/env python3
"""
E10: 真进程中断恢复 —— 子进程被 SIGKILL

为什么要单独做:
  E5 只是在同一进程内丢弃 AgentCore 对象,进程本身还活着。
  真实的"中断"是进程被 kill -9,内存状态全部消失,
  只有 SQLite 落盘的内容存活。本测试用真子进程验证这一点。

做法:
  1. 起一个子进程跑 AgentCore,写到第 N 步后 os.kill(pid, SIGKILL)
  2. 主进程重新起一个 AgentCore,从 DB 继续
  3. 校验:kill 前的数据没丢 + 能跑完
"""
import json
import os
import signal
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).parent
AGENT = HERE.parent.parent
sys.path.insert(0, str(HERE.parent))   # soundmirror 包
sys.path.insert(0, str(AGENT))


CHILD_SCRIPT = HERE / "_e10_child.py"


CHILD = '''
import os, sys, signal, time
sys.path.insert(0, {agent!r})
from soundmirror.core import AgentCore, InputEvent
from soundmirror.storage import StateStore

db = sys.argv[1]
kill_after = int(sys.argv[2])   # 跑到第几步自杀
tag = sys.argv[3]

steps = [
    ("S1", "text", {{"text": "海边六点的柴油味"}}),
    ("S2", "text", {{"text": "妈妈电话里的咳嗽"}}),
    ("S3", "action", {{"action_id": "archaeology.confirm"}}),
    ("S4", "action", {{"action_id": "identity.keep", "action_payload": {{"slot": "A"}}}}),
    ("S5", "action", {{"action_id": "variable.confirm"}}),
    ("S6", "action", {{"action_id": "proposal.confirm"}}),
    ("S7", "action", {{"action_id": "highlight.select", "action_payload": {{"slot": "A"}}}}),
    ("S8", "action", {{"action_id": "full.confirm"}}),
    ("S9", "action", {{"action_id": "profile.archive", "confirm_risk": True}}),
]

core = AgentCore(store=StateStore(db))
for i, (sid, kind, kw) in enumerate(steps, 1):
    d = core.handle(InputEvent(sid, kind, "u", "c", **kw))
    print(f"[{{tag}}] step{{i}} {{sid}} -> {{d.kind}} stage={{d.stage}}", flush=True)
    if i == kill_after:
        print(f"[{{tag}}] SIGKILL self at step {{i}}", flush=True)
        os.kill(os.getpid(), signal.SIGKILL)   # 真杀,不执行 finally
'''


def read_state(db):
    if not os.path.exists(db):
        return None
    conn = sqlite3.connect(db)
    try:
        row = conn.execute(
            "SELECT creative_session_id, state_revision, ledger "
            "FROM creative_sessions LIMIT 1"
        ).fetchone()
    except sqlite3.OperationalError:
        return None
    finally:
        conn.close()
    if not row:
        return None
    return {"cs_id": row[0], "rev": row[1], "ledger": json.loads(row[2])}


def run():
    from soundmirror.evidence.collect_v0_evidence import Claims

    claims = Claims()
    results = []

    # 写子进程脚本
    CHILD_SCRIPT.write_text(CHILD.format(agent=str(AGENT)), encoding="utf-8")

    full_steps = 9
    for kill_after in [2, 4, 6]:
        db = str(HERE / "artifacts" / f"e10_kill{kill_after}.sqlite")
        if os.path.exists(db):
            os.unlink(db)

        # Phase 1:真子进程跑到第 N 步自杀
        proc = subprocess.Popen(
            [sys.executable, str(CHILD_SCRIPT), db, str(kill_after), "p1"],
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True,
        )
        try:
            out, _ = proc.communicate(timeout=180)
        except subprocess.TimeoutExpired:
            proc.kill()
            out, _ = proc.communicate()
        rc = proc.returncode

        killed_by_signal = (rc == -signal.SIGKILL)   # -9

        at_kill = read_state(db)
        facts_at_kill = len((at_kill or {}).get("ledger", {}).get("archaeology_facts") or [])
        stage_at_kill = (at_kill or {}).get("ledger", {}).get("current_stage")

        # Phase 2:主进程新起 AgentCore,继续跑完
        from soundmirror.core import AgentCore, InputEvent
        from soundmirror.storage import StateStore
        core = AgentCore(store=StateStore(db))
        steps = [
            ("S3", "action", {"action_id": "archaeology.confirm"}),
            ("S4", "action", {"action_id": "identity.keep", "action_payload": {"slot": "A"}}),
            ("S5", "action", {"action_id": "variable.confirm"}),
            ("S6", "action", {"action_id": "proposal.confirm"}),
            ("S7", "action", {"action_id": "highlight.select", "action_payload": {"slot": "A"}}),
            ("S8", "action", {"action_id": "full.confirm"}),
            ("S9", "action", {"action_id": "profile.archive", "confirm_risk": True}),
        ]
        for sid, kind, kw in steps[kill_after - 1:]:
            d = core.handle(InputEvent(sid + "_r", kind, "u", "c", **kw))

        after = read_state(db)
        lg = (after or {}).get("ledger", {})
        facts_after = len(lg.get("archaeology_facts") or [])

        results.append({
            "kill_after_step": kill_after,
            "child_exit_code": rc,
            "killed_by_sigkill": killed_by_signal,
            "child_output_tail": out.strip().splitlines()[-1] if out.strip() else "",
            "stage_at_kill": stage_at_kill,
            "facts_at_kill": facts_at_kill,
            "facts_after_recovery": facts_after,
            "facts_lost": facts_after < facts_at_kill,
            "final_stage": lg.get("current_stage"),
            "profile_written": bool(lg.get("personal_style_profile")),
        })

    # ---- 断言 ----
    sigkilled = [r for r in results if r["killed_by_sigkill"]]
    claims.add("E10-01", "hard-recovery",
               "子进程确实被 SIGKILL(-9)杀死,不是正常退出",
               len(results), len(sigkilled),
               len(sigkilled) == len(results),
               {"exit_codes": [r["child_exit_code"] for r in results]})

    claims.add("E10-02", "hard-recovery",
               "被 kill 的子进程已把数据落盘(有素材可保)",
               3, sum(1 for r in results if r["facts_at_kill"] > 0),
               all(r["facts_at_kill"] > 0 for r in results),
               {"facts_at_kill": [r["facts_at_kill"] for r in results]})

    claims.add("E10-03", "hard-recovery",
               "kill 前素材在新进程里一条不丢",
               [r["facts_at_kill"] for r in results],
               [r["facts_after_recovery"] for r in results],
               not any(r["facts_lost"] for r in results),
               {"before": [r["facts_at_kill"] for r in results],
                "after": [r["facts_after_recovery"] for r in results]})

    claims.add("E10-04", "hard-recovery",
               "新进程能接着跑完到 ARCHIVE 并归档",
               3, sum(1 for r in results if r["profile_written"]),
               all(r["profile_written"] for r in results),
               {"final_stages": [r["final_stage"] for r in results]})

    # 重复 session 检查(必须在断言前算,不能事后改断言)
    dup_detail = []
    for kill_after in [2, 4, 6]:
        db = str(HERE / "artifacts" / f"e10_kill{kill_after}.sqlite")
        conn = sqlite3.connect(db)
        n = conn.execute("SELECT COUNT(*) FROM creative_sessions").fetchone()[0]
        conn.close()
        dup_detail.append({"kill_after": kill_after, "session_count": n})

    claims.add("E10-05", "hard-recovery",
               "恢复后没有产生重复 session(仍只有 1 个)",
               [1, 1, 1], [d["session_count"] for d in dup_detail],
               all(d["session_count"] == 1 for d in dup_detail),
               {"note": "新进程应复用 kill 前的 session,而不是新建",
                "detail": dup_detail})

    if CHILD_SCRIPT.exists():
        CHILD_SCRIPT.unlink()

    return claims, results


if __name__ == "__main__":
    c, res = run()
    for r in res:
        print(f"kill@{r['kill_after_step']}: rc={r['child_exit_code']} "
              f"(SIGKILL={r['killed_by_sigkill']}) "
              f"facts {r['facts_at_kill']}→{r['facts_after_recovery']} "
              f"final={r['final_stage']} profile={r['profile_written']}")
    s = c.summary()
    print(f"\n通过 {s['passed']}/{s['total']}")
    for cl in c.claims:
        if not cl["passed"]:
            print(f"  ✗ {cl['claim_id']}: {cl['statement']} | exp={cl['expected']} act={cl['actual']}")
    sys.exit(0 if s["failed"] == 0 else 1)
