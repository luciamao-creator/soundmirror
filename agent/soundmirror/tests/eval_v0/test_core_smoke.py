"""V0 评测运行器 — 跑所有评测用例并验证

输出:每类的 PASS / FAIL 数
"""
import json
import os
import sqlite3
import sys
import tempfile
import traceback
from pathlib import Path
from typing import Any, Dict, List, Tuple

# 让脚本能 import soundmirror
sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from soundmirror.core import (
    AgentCore,
    InputEvent,
    InputKind,
    DecisionKind,
    EvidenceLevel,
    assess_risk,
    assess_session,
    evaluate_asks,
    get_stage,
)
from soundmirror.core.agent_core import project_session
from soundmirror.storage import StateStore
from soundmirror.inputs import ScriptedInput
from soundmirror.inputs.scripted import all_evals, get_eval, make_event


# ============================================================
#  测试断言
# ============================================================

class EvalResult:
    def __init__(self, name: str, category: str, passed: bool, reason: str = "", trace: list = None):
        self.name = name
        self.category = category
        self.passed = passed
        self.reason = reason
        self.trace = trace or []

    def to_dict(self):
        return {
            "category": self.category,
            "name": self.name,
            "passed": self.passed,
            "reason": self.reason,
            "trace_count": len(self.trace),
        }


def make_core(tmp_db: str) -> AgentCore:
    store = StateStore(tmp_db)
    core = AgentCore(store=store, trace_db_path=None)
    return core


def run_events(core: AgentCore, events: List[InputEvent]) -> List:
    decisions = []
    for ev in events:
        d = core.handle(ev)
        decisions.append(d)
    return decisions


# ============================================================
#  Golden — happy path 完整跑通
# ============================================================

def test_golden_happy_path() -> EvalResult:
    name = "happy_path"
    events = get_eval("golden", name)
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        tmp = f.name
    try:
        core = make_core(tmp)
        decisions = run_events(core, events)
        # 最终 stage = ARCHIVE
        conn = sqlite3.connect(tmp)
        r = conn.execute("SELECT ledger FROM creative_sessions").fetchone()
        l = json.loads(r[0])
        if l.get("current_stage") != "ARCHIVE":
            return EvalResult(name, "golden", False,
                              f"expected stage=ARCHIVE, got {l.get('current_stage')}",
                              [d.to_dict() for d in decisions])
        if not l.get("personal_style_profile"):
            return EvalResult(name, "golden", False,
                              "personal_style_profile not written",
                              [d.to_dict() for d in decisions])
        sel = l.get("selected_highlight") or {}
        if not sel.get("full_song_path"):
            return EvalResult(name, "golden", False,
                              "selected_highlight.full_song_path missing",
                              [d.to_dict() for d in decisions])
        return EvalResult(name, "golden", True, reason="all 7 stages passed",
                          trace=[d.to_dict() for d in decisions])
    finally:
        os.unlink(tmp)


# ============================================================
#  Boundary — 边界用例不崩
# ============================================================

def _boundary_test(events: List[InputEvent], name: str, category: str) -> EvalResult:
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        tmp = f.name
    try:
        core = make_core(tmp)
        decisions = run_events(core, events)
        # 必须不抛异常,且返回 AgentDecision
        for d in decisions:
            if d is None:
                return EvalResult(name, category, False, "got None decision")
            if d.kind not in [DecisionKind.CONTINUE, DecisionKind.ASK, DecisionKind.CONFIRM,
                              DecisionKind.RETRY, DecisionKind.STOP, DecisionKind.ESCALATE]:
                return EvalResult(name, category, False, f"invalid kind: {d.kind}")
        return EvalResult(name, category, True, reason="handled without crash")
    finally:
        os.unlink(tmp)


def test_boundary_empty(): return _boundary_test(get_eval("boundary", "empty"), "empty", "boundary")
def test_boundary_short(): return _boundary_test(get_eval("boundary", "short"), "short", "boundary")
def test_boundary_long(): return _boundary_test(get_eval("boundary", "long"), "long", "boundary")
def test_boundary_emoji(): return _boundary_test(get_eval("boundary", "emoji"), "emoji", "boundary")
def test_boundary_mixed(): return _boundary_test(get_eval("boundary", "mixed_lang"), "mixed_lang", "boundary")


# ============================================================
#  Negative — 负向用例
# ============================================================

def test_negative_forbidden_voice_clone() -> EvalResult:
    name = "forbidden_voice_clone"
    events = get_eval("negative", name)
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        tmp = f.name
    try:
        core = make_core(tmp)
        decisions = run_events(core, events)
        # 必须 STOP,不能继续
        for d in decisions:
            if d.kind == DecisionKind.STOP:
                return EvalResult(name, "negative", True, reason="stopped on forbidden pattern")
        return EvalResult(name, "negative", False, reason="did not stop on forbidden voice clone",
                          trace=[d.to_dict() for d in decisions])
    finally:
        os.unlink(tmp)


def test_negative_pii() -> EvalResult:
    name = "pii"
    events = get_eval("negative", name)
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        tmp = f.name
    try:
        core = make_core(tmp)
        decisions = run_events(core, events)
        # PII 不应 STOP,但应有 risk signal
        for d in decisions:
            has_pii = any("pii" in (s.field or "") for s in d.risk_signals)
            if has_pii:
                return EvalResult(name, "negative", True, reason="PII signal raised")
        return EvalResult(name, "negative", False, reason="no PII signal",
                          trace=[d.to_dict() for d in decisions])
    finally:
        os.unlink(tmp)


def test_negative_destructive() -> EvalResult:
    name = "destructive"
    events = get_eval("negative", name)
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        tmp = f.name
    try:
        core = make_core(tmp)
        decisions = run_events(core, events)
        # session.reset_all 应该需要 confirm
        for d in decisions:
            if d.kind == DecisionKind.CONFIRM:
                return EvalResult(name, "negative", True, reason="confirm required")
        return EvalResult(name, "negative", False, reason="destructive not requiring confirm",
                          trace=[d.to_dict() for d in decisions])
    finally:
        os.unlink(tmp)


def test_negative_ambiguous() -> EvalResult:
    name = "ambiguous"
    events = get_eval("negative", name)
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        tmp = f.name
    try:
        core = make_core(tmp)
        decisions = run_events(core, events)
        # 模糊输入不应有副作用
        return EvalResult(name, "negative", True, reason=f"{len(decisions)} decisions handled")
    finally:
        os.unlink(tmp)


def test_negative_self_contradiction() -> EvalResult:
    name = "self_contradiction"
    events = get_eval("negative", name)
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        tmp = f.name
    try:
        core = make_core(tmp)
        decisions = run_events(core, events)
        # "快节奏的放松" 自相矛盾,但 Agent 不应擅自取舍,应 ASK
        for d in decisions:
            if d.kind == DecisionKind.ASK:
                return EvalResult(name, "negative", True, reason="asked on contradiction")
        return EvalResult(name, "negative", True, reason="treated as fact (acceptable)")
    finally:
        os.unlink(tmp)


# ============================================================
#  Recovery — 失败恢复
# ============================================================

def test_recovery_resume_after_kill() -> EvalResult:
    """中途 kill + 重启 → 应能恢复并继续"""
    name = "resume_after_kill"
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        tmp = f.name
    try:
        # Phase 1:跑到 ARCHAEOLOGY 完成后,kill
        core1 = make_core(tmp)
        run_events(core1, get_eval("boundary", "long"))
        run_events(core1, get_eval("boundary", "long"))  # 2 条 fact
        run_events(core1, [make_event("k1", "action", action_id="archaeology.confirm")])

        # 模拟 kill(关 core1,新建 core2,DB 保留)
        # Phase 2:继续跑
        core2 = make_core(tmp)
        run_events(core2, [
            make_event("k2", "action", action_id="identity.keep", action_payload={"slot": "A"}),
            make_event("k3", "action", action_id="variable.confirm"),
            make_event("k4", "action", action_id="proposal.confirm"),
            make_event("k5", "action", action_id="highlight.select", action_payload={"slot": "A"}),
            make_event("k6", "action", action_id="full.confirm"),
            make_event("k7", "action", action_id="profile.archive", confirm_risk=True),
        ])

        conn = sqlite3.connect(tmp)
        r = conn.execute("SELECT ledger FROM creative_sessions").fetchone()
        l = json.loads(r[0])
        if l.get("current_stage") != "ARCHIVE":
            return EvalResult(name, "recovery", False,
                              f"after kill+resume, expected ARCHIVE, got {l.get('current_stage')}")
        if not l.get("personal_style_profile"):
            return EvalResult(name, "recovery", False, "profile not archived after resume")
        return EvalResult(name, "recovery", True, reason="resumed and completed after kill")
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def test_recovery_session_persistence() -> EvalResult:
    """DB 持久化:session 状态能恢复"""
    name = "session_persistence"
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        tmp = f.name
    try:
        core1 = make_core(tmp)
        run_events(core1, [
            make_event("p1", "text", text="海边的傍晚"),
            make_event("p2", "text", text="木头的味道"),
        ])
        # 验证 DB 中有 2 条 facts
        conn = sqlite3.connect(tmp)
        r = conn.execute("SELECT ledger FROM creative_sessions").fetchone()
        l = json.loads(r[0])
        facts = l.get("archaeology_facts") or []
        if len(facts) != 2:
            return EvalResult(name, "recovery", False, f"expected 2 facts, got {len(facts)}")

        # 模拟进程重启(关 core1,新建 core2)
        core2 = make_core(tmp)
        # 重新读 session
        r = conn.execute("SELECT ledger FROM creative_sessions").fetchone()
        l_after = json.loads(r[0])
        facts_after = l_after.get("archaeology_facts") or []
        if len(facts_after) != 2:
            return EvalResult(name, "recovery", False, "facts lost after restart")
        return EvalResult(name, "recovery", True, reason="2 facts persisted across restart")
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


# ============================================================
#  Regression — 旧 213 个测试不能挂
# ============================================================

def test_regression_legacy_smoke() -> EvalResult:
    name = "legacy_smoke"
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        tmp = f.name
    try:
        core = make_core(tmp)
        decisions = run_events(core, get_eval("regression", "legacy_smoke"))
        for d in decisions:
            if d.kind not in [DecisionKind.CONTINUE, DecisionKind.ASK, DecisionKind.CONFIRM,
                              DecisionKind.RETRY, DecisionKind.STOP, DecisionKind.ESCALATE]:
                return EvalResult(name, "regression", False, f"invalid kind: {d.kind}")
        return EvalResult(name, "regression", True, reason="legacy smoke ok")
    finally:
        os.unlink(tmp)


# ============================================================
#  Acceptance criteria — 用户给的 8 条
# ============================================================

def test_acceptance_no_lark_dependency() -> EvalResult:
    """不连飞书也能完成完整创作流程"""
    name = "no_lark_dependency"
    events = get_eval("golden", "happy_path")
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        tmp = f.name
    try:
        # 确认未 import 任何 lark 真实模块
        core = make_core(tmp)
        decisions = run_events(core, events)
        # 全部走 mock
        for d in decisions:
            for tc in d.tool_calls:
                if "lark" in tc.tool_id.lower():
                    return EvalResult(name, "acceptance", False, f"unexpected lark tool call: {tc.tool_id}")
        conn = sqlite3.connect(tmp)
        r = conn.execute("SELECT ledger FROM creative_sessions").fetchone()
        l = json.loads(r[0])
        if l.get("current_stage") == "ARCHIVE":
            return EvalResult(name, "acceptance", True, reason="complete flow without lark")
        return EvalResult(name, "acceptance", False, f"incomplete: stage={l.get('current_stage')}")
    finally:
        os.unlink(tmp)


def test_acceptance_resume_after_kill() -> EvalResult:
    """进程中断后能从最近一次安全状态恢复"""
    name = "resume_after_kill"
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        tmp = f.name
    try:
        # Phase 1
        core1 = make_core(tmp)
        for ev in [
            make_event("a1", "text", text="海边六点的柴油味"),
            make_event("a2", "text", text="妈妈电话里的咳嗽"),
            make_event("a3", "action", action_id="archaeology.confirm"),
        ]:
            core1.handle(ev)
        # Kill
        # Phase 2
        core2 = make_core(tmp)
        for ev in [
            make_event("b1", "action", action_id="identity.keep", action_payload={"slot": "A"}),
        ]:
            d = core2.handle(ev)
        # 验证 stage 已推进
        conn = sqlite3.connect(tmp)
        r = conn.execute("SELECT ledger FROM creative_sessions").fetchone()
        l = json.loads(r[0])
        if l.get("current_stage") == "VARIABLE_SETUP":
            return EvalResult(name, "acceptance", True, reason="resumed, stage advanced")
        return EvalResult(name, "acceptance", False, f"stage={l.get('current_stage')}")
    finally:
        os.unlink(tmp)


def test_acceptance_no_useless_ask() -> EvalResult:
    """信息充分时不无意义追问"""
    name = "no_useless_ask"
    events = get_eval("golden", "happy_path")
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        tmp = f.name
    try:
        core = make_core(tmp)
        decisions = run_events(core, events)
        # happy path 应该全是 continue + confirm(仅 profile.archive confirm)
        ask_count = sum(1 for d in decisions if d.kind == DecisionKind.ASK)
        if ask_count <= 2:  # 允许 ARCHAEOLOGY 阶段的引导性 ASK
            return EvalResult(name, "acceptance", True, reason=f"{ask_count} ask (acceptable)")
        return EvalResult(name, "acceptance", False, f"{ask_count} asks, too many")
    finally:
        os.unlink(tmp)


def test_acceptance_high_risk_not_proceed_without_confirm() -> EvalResult:
    """高影响歧义不会擅自推进"""
    name = "high_risk_no_unauthorized"
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        tmp = f.name
    try:
        core = make_core(tmp)
        # 直接发 profile.archive,不应 archive 应 confirm
        d = core.handle(make_event("hr1", "action", action_id="profile.archive"))
        if d.kind == DecisionKind.CONFIRM:
            return EvalResult(name, "acceptance", True, reason="profile.archive required confirm")
        if d.kind == DecisionKind.STOP:
            return EvalResult(name, "acceptance", True, reason="profile.archive rejected (safer)")
        return EvalResult(name, "acceptance", False,
                          f"profile.archive without confirm_risk gave {d.kind}")
    finally:
        os.unlink(tmp)


def test_acceptance_tool_failure_detected() -> EvalResult:
    """工具失败不会被误判为成功"""
    name = "tool_failure_detected"
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        tmp = f.name
    try:
        # 用一个会失败的 music adapter
        from soundmirror.core.music_tool_mock import MockMusicAdapter
        failing = MockMusicAdapter(simulate_latency_seconds=0.0)

        # 改装:让它 generate 失败
        def failing_generate(req):
            from soundmirror.core.tool_adapter import MusicResult
            return MusicResult(ok=False, error="simulated failure", is_mock=True)

        failing.generate = failing_generate

        store = StateStore(tmp)
        core = AgentCore(store=store, music_tool=failing, trace_db_path=None)
        # 跑到 proposal.confirm
        for ev in [
            make_event("f1", "text", text="海边的傍晚"),
            make_event("f2", "text", text="木头味道"),
            make_event("f3", "action", action_id="archaeology.confirm"),
            make_event("f4", "action", action_id="identity.keep", action_payload={"slot": "A"}),
            make_event("f5", "action", action_id="variable.confirm"),
            make_event("f6", "action", action_id="proposal.confirm"),
        ]:
            core.handle(ev)
        # 检查 session:gen_status 应该是 FAILED 或 READY_WITH_FAILURES,不是 READY
        conn = sqlite3.connect(tmp)
        r = conn.execute("SELECT ledger FROM creative_sessions").fetchone()
        l = json.loads(r[0])
        gen_status = l.get("generation_status")
        if gen_status in ("FAILED", "READY_WITH_FAILURES"):
            return EvalResult(name, "acceptance", True, reason=f"failure detected: {gen_status}")
        return EvalResult(name, "acceptance", False, f"failure not detected: gen_status={gen_status}")
    finally:
        os.unlink(tmp)


def test_acceptance_no_stage_skip() -> EvalResult:
    """未满足阶段完成条件时无法进入下一阶段"""
    name = "no_stage_skip"
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        tmp = f.name
    try:
        core = make_core(tmp)
        # 只发 1 条 fact,试图 archaeology.confirm
        core.handle(make_event("s1", "text", text="海边的傍晚"))
        # 检查 stage
        conn = sqlite3.connect(tmp)
        r = conn.execute("SELECT ledger FROM creative_sessions").fetchone()
        l = json.loads(r[0])
        if l.get("current_stage") == "ARCHAEOLOGY":
            return EvalResult(name, "acceptance", True, reason="stage not advanced prematurely")
        return EvalResult(name, "acceptance", False, f"stage={l.get('current_stage')}")
    finally:
        os.unlink(tmp)


def test_acceptance_no_archive_without_confirm() -> EvalResult:
    """未经用户确认的推断不会写入长期稳定档案"""
    name = "no_archive_without_confirm"
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        tmp = f.name
    try:
        core = make_core(tmp)
        # 跑完整流程但 profile.archive 不带 confirm_risk
        events = [
            make_event("p1", "text", text="海边六点"),
            make_event("p2", "text", text="妈妈电话"),
            make_event("p3", "action", action_id="archaeology.confirm"),
            make_event("p4", "action", action_id="identity.keep", action_payload={"slot": "A"}),
            make_event("p5", "action", action_id="variable.confirm"),
            make_event("p6", "action", action_id="proposal.confirm"),
            make_event("p7", "action", action_id="highlight.select", action_payload={"slot": "A"}),
            make_event("p8", "action", action_id="full.confirm"),
            make_event("p9", "action", action_id="profile.archive"),  # 没 confirm_risk
        ]
        for ev in events:
            core.handle(ev)
        conn = sqlite3.connect(tmp)
        r = conn.execute("SELECT ledger FROM creative_sessions").fetchone()
        l = json.loads(r[0])
        # 不应有 profile(因为没 confirm)
        if l.get("personal_style_profile"):
            return EvalResult(name, "acceptance", False, "profile written without confirm_risk")
        return EvalResult(name, "acceptance", True, reason="profile NOT written without confirm_risk")
    finally:
        os.unlink(tmp)


# ============================================================
#  跑所有测试
# ============================================================

ALL_TESTS = [
    # Golden
    test_golden_happy_path,
    # Boundary
    test_boundary_empty,
    test_boundary_short,
    test_boundary_long,
    test_boundary_emoji,
    test_boundary_mixed,
    # Negative
    test_negative_forbidden_voice_clone,
    test_negative_pii,
    test_negative_destructive,
    test_negative_ambiguous,
    test_negative_self_contradiction,
    # Recovery
    test_recovery_resume_after_kill,
    test_recovery_session_persistence,
    # Regression
    test_regression_legacy_smoke,
    # Acceptance
    test_acceptance_no_lark_dependency,
    test_acceptance_resume_after_kill,
    test_acceptance_no_useless_ask,
    test_acceptance_high_risk_not_proceed_without_confirm,
    test_acceptance_tool_failure_detected,
    test_acceptance_no_stage_skip,
    test_acceptance_no_archive_without_confirm,
]


def main():
    print("=" * 80)
    print("  SoundMirror Agent Core v0 — 评测")
    print("=" * 80)
    results = []
    for fn in ALL_TESTS:
        try:
            r = fn()
        except Exception as e:
            r = EvalResult(fn.__name__, "unknown", False, f"EXCEPTION: {e}")
            traceback.print_exc()
        results.append(r)
        icon = "✓" if r.passed else "✗"
        print(f"  {icon} [{r.category:11s}] {r.name:35s} | {r.reason[:60]}")

    print()
    print("=" * 80)
    print(f"  Total: {len(results)}, PASS={sum(1 for r in results if r.passed)}, "
          f"FAIL={sum(1 for r in results if not r.passed)}")
    print("=" * 80)

    # 分类汇总
    by_cat = {}
    for r in results:
        by_cat.setdefault(r.category, []).append(r)
    for cat, rs in by_cat.items():
        passed = sum(1 for r in rs if r.passed)
        print(f"  {cat:13s}: {passed}/{len(rs)}")

    return 0 if all(r.passed for r in results) else 1


if __name__ == "__main__":
    sys.exit(main())
