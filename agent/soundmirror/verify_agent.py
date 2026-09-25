"""
verify_agent.py — 一站式 Agent 能力验证

不改业务代码,只:
  1. 跑 CLI demo,happy path 全链路
  2. 验证每个 step 的卡 JSON 符合 Lark v1
  3. 检查最后的 ledger + audit log 是否符合预期
  4. 跑全部 golden cases(单元 + 集成)
  5. 真调一次 Matrix,如果可用

输出:
  - 控制台:每步 PASS/FAIL
  - 文件: out/verify_report.json(完整结果)
"""
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Optional

ROOT = Path(__file__).parent
# project root = voice-persona-assessment/
PROJECT_ROOT = ROOT.parent.parent
FIXTURES_DIR = ROOT / "fixtures"
OUT_DIR = ROOT / "out"
REPORT_PATH = OUT_DIR / "verify_report.json"


def verify_lark_card(card: dict) -> tuple[bool, list[str]]:
    """校验 Lark v1 schema"""
    issues = []
    if not isinstance(card, dict):
        return False, ["card is not a dict"]
    for required in ("config", "header", "elements"):
        if required not in card:
            issues.append(f"missing field: {required}")
    header = card.get("header", {})
    if not isinstance(header, dict) or "title" not in header:
        issues.append("header.title missing")
    title = header.get("title", {})
    if isinstance(title, dict) and title.get("tag") != "plain_text":
        issues.append("header.title.tag != plain_text")
    template = header.get("template")
    if template not in (
        "blue", "green", "red", "yellow", "indigo", "purple", "grey", "orange"
    ):
        issues.append(f"header.template invalid: {template}")
    elements = card.get("elements", [])
    if not isinstance(elements, list):
        issues.append("elements not list")
        return len(issues) == 0, issues
    for i, elem in enumerate(elements):
        if not isinstance(elem, dict):
            issues.append(f"element[{i}] not dict")
            continue
        tag = elem.get("tag")
        if tag == "action":
            for j, btn in enumerate(elem.get("actions", [])):
                if "text" not in btn:
                    issues.append(f"button[{i}.{j}] missing text")
                if "value" not in btn or "action_id" not in btn.get("value", {}):
                    issues.append(f"button[{i}.{j}] missing action_id")
                btn_type = btn.get("type")
                if btn_type not in ("primary", "danger", "default", None):
                    issues.append(f"button[{i}.{j}] type invalid: {btn_type}")
    return len(issues) == 0, issues


def run_journey_replay(fixture: Path, out_subdir: str) -> dict:
    """run CLI replay fixture, return summary"""
    out_path = OUT_DIR / out_subdir
    if out_path.exists():
        import shutil
        shutil.rmtree(out_path, ignore_errors=True)

    # 用 import path 而非包名
    env = os.environ.copy()
    env["PYTHONPATH"] = str(PROJECT_ROOT / "agent") + ":" + env.get("PYTHONPATH", "")
    cmd = [
        sys.executable, "-c",
        f"import sys; sys.path.insert(0, '{PROJECT_ROOT}/agent'); "
        f"from soundmirror import cli; sys.argv = ['cli', 'replay', '{fixture}', "
        f"'--out-dir', '{out_path}']; cli.main()"
    ]
    result = subprocess.run(cmd, capture_output=True, text=True, cwd=str(PROJECT_ROOT), env=env)
    return _summarize_journey(out_path, exit_code=result.returncode, stderr=result.stderr)


def _summarize_journey(out_path: Path, exit_code: int = 0, stderr: str = "") -> dict:
    """读 journey 目录,生成可读摘要"""
    if exit_code != 0:
        return {
            "ok": False, "exit_code": exit_code,
            "stderr": stderr[-500:] if stderr else "",
        }
    if not out_path.exists():
        return {"ok": False, "error": "out_path missing"}

    # 收集产出的卡
    card_files = sorted([f for f in out_path.glob("step_*.json")
                         if "envelope" not in f.name])
    cards_ok = 0
    cards_bad = []
    for cf in card_files:
        try:
            card = json.loads(cf.read_text(encoding="utf-8"))
            ok, issues = verify_lark_card(card)
            if ok:
                cards_ok += 1
            else:
                cards_bad.append({"file": cf.name, "issues": issues})
        except Exception as e:
            cards_bad.append({"file": cf.name, "issues": [str(e)]})

    final_state = json.loads((out_path / "final_state.json").read_text(encoding="utf-8")) \
        if (out_path / "final_state.json").exists() else {}
    audit_log = json.loads((out_path / "audit_log.json").read_text(encoding="utf-8")) \
        if (out_path / "audit_log.json").exists() else []

    return {
        "ok": len(cards_bad) == 0,
        "total_cards": len(card_files),
        "cards_passed": cards_ok,
        "cards_failed": cards_bad,
        "final_state": final_state,
        "audit_event_count": len(audit_log),
        "audit_event_types": list(set(a["event_type"] for a in audit_log)),
    }


def run_golden_cases() -> dict:
    """pytest all tests"""
    pkg_root = str(PROJECT_ROOT / "agent")
    env = os.environ.copy()
    env["PYTHONPATH"] = pkg_root + ":" + env.get("PYTHONPATH", "")
    cmd = [sys.executable, "-m", "pytest", "soundmirror/tests", "-q", "--tb=line"]
    result = subprocess.run(cmd, capture_output=True, text=True, cwd=pkg_root, env=env)
    output = result.stdout + result.stderr

    # 解析最后一行 "X passed, Y failed"
    summary = ""
    for line in output.splitlines()[::-1]:
        if "passed" in line or "failed" in line:
            summary = line.strip()
            break

    return {
        "ok": result.returncode == 0,
        "exit_code": result.returncode,
        "summary": summary,
        "stderr_tail": result.stderr[-300:] if result.stderr else "",
    }


def try_real_matrix() -> dict:
    """尝试真调 Matrix(短超时,失败不算 verify 失败)"""
    env = os.environ.copy()
    env["PYTHONPATH"] = str(PROJECT_ROOT / "agent") + ":" + env.get("PYTHONPATH", "")
    prompt = "A peaceful acoustic folk ballad with warm vocals and gentle acoustic guitar"
    lyrics = "[verse]\\nWalking down the dusty road\\nThe sun is fading"
    cmd = [
        sys.executable, "-c",
        f"import sys; sys.path.insert(0, '{PROJECT_ROOT}/agent'); "
        f"from soundmirror import cli; sys.argv = ['cli', 'generate-music', "
        f"'--prompt', '{prompt}', '--lyrics', '{lyrics}']; cli.main()"
    ]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, cwd=str(PROJECT_ROOT), env=env, timeout=30)
    except subprocess.TimeoutExpired:
        return {"ok": False, "stdout": "", "stderr": "matrix timeout after 30s (likely not auth'd)", "skipped": True}
    if not result.stdout and not result.stderr:
        return {"ok": False, "stdout": "", "stderr": "no output", "skipped": True}
    return {
        "ok": result.returncode == 0,
        "stdout": result.stdout[-1000:],
        "stderr": result.stderr[-500:] if result.stderr else "",
    }


def main():
    """CLI entry"""
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--run-matrix", action="store_true", help="跑真实 Matrix 调用(默认 skip,慢)")
    args = parser.parse_args()
    main_impl(args)


def main_impl(args):
    OUT_DIR.mkdir(parents=True, exist_ok=True)

    print("\n=== SoundMirror Agent Verification ===\n")
    report = {
        "timestamp": "2026-09-17",
        "version": "1.1.0",
        "matrix_attempted": getattr(args, "run_matrix", False),
        "sections": {},
    }

    # 1. Golden cases (unit + integration)
    print("[1/4] Running golden cases...")
    gc = run_golden_cases()
    report["sections"]["golden_cases"] = gc
    print(f"     {gc['summary']}")
    print()

    # 2. Replay fixtures — content output correctness
    print("[2/4] Replaying content output fixtures...")
    fixtures = [
        (FIXTURES_DIR / "journey_01_archaeology.json", "j01_archaeology"),
        (FIXTURES_DIR / "journey_02_high_risk.json", "j02_high_risk"),
        (FIXTURES_DIR / "journey_03_destructive.json", "j03_destructive"),
        (FIXTURES_DIR / "journey_04_clarification.json", "j04_clarification"),
        (FIXTURES_DIR / "journey_05_full_lifecycle.json", "j05_full_lifecycle"),
        (FIXTURES_DIR / "journey_06_action_coverage.json", "j06_action_coverage"),
        (FIXTURES_DIR / "journey_07_edge_cases.json", "j07_edge_cases"),
        (FIXTURES_DIR / "journey_08_variable_setup.json", "j08_variable_setup"),
        (FIXTURES_DIR / "journey_09_full_flow.json", "j09_full_flow"),
        (FIXTURES_DIR / "journey_10_proposal_lyric.json", "j10_proposal_lyric"),
    ]
    section = {}
    for fixture_path, subdir in fixtures:
        if not fixture_path.exists():
            section[subdir] = {"ok": False, "error": "fixture not found"}
            continue
        print(f"     • {fixture_path.name}...")
        r = run_journey_replay(fixture_path, subdir)
        section[subdir] = r
        status = "✓" if r["ok"] else "✗"
        print(f"       {status} {r.get('total_cards', 0)} cards, "
              f"{r.get('cards_passed', 0)} passed, "
              f"audit={r.get('audit_event_count', 0)} events")
    report["sections"]["content_replays"] = section
    print()

    # 3. Real Matrix (optional,默认 skip)
    if args.run_matrix:
        print("[3/4] Real Matrix generation (optional)...")
        matrix = try_real_matrix()
        report["sections"]["real_matrix"] = matrix
        if matrix["ok"]:
            print("     ✓ Matrix succeeded:")
            for line in matrix["stdout"].splitlines()[:5]:
                print(f"       {line}")
        else:
            print("     ⚠️ Skipped (mcode-tools not available)")
    else:
        print("[3/4] Real Matrix skipped (use --run-matrix to enable)")
        report["sections"]["real_matrix"] = {"ok": None, "skipped": True}
    print()

    # 4. Final summary
    all_pass = (
        gc["ok"]
        and all(r["ok"] for r in section.values())
    )
    report["all_pass"] = all_pass
    REPORT_PATH.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")

    print(f"=== {'ALL PASS' if all_pass else 'FAILURES'} ===")
    print(f"Detailed report: {REPORT_PATH}\n")

    sys.exit(0 if all_pass else 1)


if __name__ == "__main__":
    main()
