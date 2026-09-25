"""
inspect_output.py — 可视化每个 step 产出

读 out/<journey>/ 下所有 step_*.json,展示:
  - step 编号
  - 卡片标题 + 模板色
  - 按钮(action_id 列表)
  - 文本块数量
  - state_revision(对比前后)

不依赖飞书,只看内容输出。

用法:
  python inspect_output.py [out_dir]  # 默认 ./out
"""
import json
import sys
from pathlib import Path


ROOT = Path(__file__).parent
OUT_DIR = ROOT / "out"
PROJECT_ROOT = ROOT.parent.parent

TEMPLATE_GLYPHS = {
    "blue": "🔵", "green": "🟢", "red": "🔴", "yellow": "🟡",
    "indigo": "🟣", "purple": "🟪", "grey": "⚪", "orange": "🟠",
}


def inspect_step(card: dict) -> str:
    """生成单 step 的可读摘要"""
    header = card.get("header", {})
    title = header.get("title", {}).get("content", "?")
    template = header.get("template", "?")
    glyph = TEMPLATE_GLYPHS.get(template, "▫️")

    lines = [f"  {glyph} {title}"]

    elements = card.get("elements", [])
    text_blocks = [e for e in elements if e.get("tag") == "div"]
    note_blocks = [e for e in elements if e.get("tag") == "note"]
    action_blocks = [e for e in elements if e.get("tag") == "action"]

    if text_blocks:
        lines.append(f"     📝 text_blocks: {len(text_blocks)}")
    if note_blocks:
        lines.append(f"     📌 notes: {len(note_blocks)}")
    if action_blocks:
        for ab in action_blocks:
            buttons = ab.get("actions", [])
            for btn in buttons:
                label = btn.get("text", {}).get("content", "?")
                action_id = btn.get("value", {}).get("action_id", "?")
                btn_type = btn.get("type", "?")
                danger = "⚠️" if btn_type == "danger" else ""
                lines.append(f"     🔘 [{label}] {danger} → {action_id}")
    return "\n".join(lines)


def inspect_journey(journey_dir: Path) -> str:
    """读一个 journey 的所有 step + final state + audit,生成摘要"""
    if not journey_dir.exists():
        return f"❌ {journey_dir} not found"

    # 收集 step 卡(过滤 envelope)
    step_files = sorted([f for f in journey_dir.glob("step_*.json")
                         if "envelope" not in f.name])

    out = [f"\n=== {journey_dir.name} ===", f"  {len(step_files)} cards:"]

    prev_rev = None
    for sf in step_files:
        try:
            card = json.loads(sf.read_text(encoding="utf-8"))
            out.append(inspect_step(card))
        except Exception as e:
            out.append(f"  ❌ {sf.name}: {e}")

    # final state
    fs = journey_dir / "final_state.json"
    if fs.exists():
        state = json.loads(fs.read_text(encoding="utf-8"))
        out.append(f"\n  📦 final: revision={state.get('state_revision')} "
                  f"stage={state.get('ledger', {}).get('current_stage')}")
        if "archaeology_facts" in state.get("ledger", {}):
            out.append(f"     archaeology_facts: {len(state['ledger']['archaeology_facts'])} 条")
        if "persona_selections" in state.get("ledger", {}):
            out.append(f"     persona_selections: {state['ledger']['persona_selections']}")

    # audit
    audit = journey_dir / "audit_log.json"
    if audit.exists():
        events = json.loads(audit.read_text(encoding="utf-8"))
        out.append(f"\n  📜 audit: {len(events)} events")
        event_types = {}
        for e in events:
            t = e.get("event_type", "?")
            event_types[t] = event_types.get(t, 0) + 1
        for t, n in sorted(event_types.items()):
            out.append(f"     - {t}: {n}")

    return "\n".join(out)


def main():
    if len(sys.argv) > 1:
        # 支持多个 path:每个 path 可以是 journey 目录, 也可以是包含 journey 的父目录
        targets = [Path(p) for p in sys.argv[1:]]
    else:
        targets = [OUT_DIR]

    journey_dirs = []
    for t in targets:
        if not t.exists():
            print(f"❌ {t} not found")
            continue
        if t.is_dir():
            # 判断是 journey 子目录(含 step_*.json)还是父目录
            has_step = any(t.glob("step_*[!_envelope].json"))
            if has_step:
                journey_dirs.append(t)
            else:
                journey_dirs.extend(sorted([d for d in t.iterdir() if d.is_dir()]))
    if not journey_dirs:
        print(f"No journeys found")
        sys.exit(1)

    for jd in journey_dirs:
        print(inspect_journey(jd))

    # 整体摘要
    print("\n" + "=" * 60)
    print(f"Total: {len(journey_dirs)} journeys inspected")
    print(f"  Paths: {', '.join(str(t.absolute()) for t in targets)}")
    print(f"\n下一步可以:")
    print(f"  1. 看单个卡:  cat <journey>/step_NNN_*.json")
    print(f"  2. 看 ledger: cat <journey>/final_state.json")
    print(f"  3. 看 audit:  cat <journey>/audit_log.json")


if __name__ == "__main__":
    main()
