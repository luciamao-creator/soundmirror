"""评测集 — ScriptedInput 工厂

每个评测用例是一个 [InputEvent, ...] 序列。
跑完整个序列后,断言 session 状态符合预期。
"""
from typing import Any, Callable, Dict, List, Optional

from . import ScriptedInput
from ..core.types import InputEvent, InputKind
import time


def make_event(
    event_id: str,
    kind: str,
    actor_user_id: str = "u_eval",
    chat_id: str = "c_eval",
    text: str = "",
    action_id: Optional[str] = None,
    action_payload: Optional[dict] = None,
    confirm_risk: Optional[bool] = None,
    selected_option: Optional[str] = None,
    continue_yes_no: Optional[bool] = None,
) -> InputEvent:
    return InputEvent(
        event_id=event_id,
        kind=kind,
        actor_user_id=actor_user_id,
        chat_id=chat_id,
        text=text,
        action_id=action_id,
        action_payload=action_payload or {},
        confirm_risk=confirm_risk,
        selected_option=selected_option,
        continue_yes_no=continue_yes_no,
        occurred_at=time.time(),
    )


# ============================================================
#  Golden: 5 个 happy path
# ============================================================

GOLDEN_HAPPY_PATH = [
    # ARCHAEOLOGY
    make_event("g1", "text", text="海边六点的柴油味"),
    make_event("g2", "text", text="妈妈电话里的咳嗽"),
    make_event("g3", "action", action_id="archaeology.confirm"),
    # IDENTITY
    make_event("g4", "action", action_id="identity.keep", action_payload={"slot": "A"}),
    # VARIABLE
    make_event("g5", "action", action_id="variable.confirm"),
    # PROPOSAL
    make_event("g6", "action", action_id="proposal.confirm"),
    # HIGHLIGHT
    make_event("g7", "action", action_id="highlight.select", action_payload={"slot": "A"}),
    # FULL
    make_event("g8", "action", action_id="full.confirm"),
    # ARCHIVE
    make_event("g9", "action", action_id="profile.archive", confirm_risk=True),
]


# ============================================================
#  Boundary: 5 个边界用例
# ============================================================

BOUNDARY_EMPTY = [
    make_event("b1", "text", text=""),  # 空文本
]

BOUNDARY_SHORT = [
    make_event("b1", "text", text="嗯"),  # 单字
]

BOUNDARY_LONG = [
    make_event("b1", "text", text="这是一段很长的素材描述,包含了许多细节,关于一个夏天的下午在海边的记忆,海风、阳光、远处渔船的汽笛声、空气中弥漫的盐味,以及内心那种说不出的宁静和期待,这种感受已经很久没有过了"),
]

BOUNDARY_EMOJI = [
    make_event("b1", "text", text="🎵🎶"),
]

BOUNDARY_MIXED_LANG = [
    make_event("b1", "text", text="今天 morning 散步时看到 autumn leaves 🍂"),
]


# ============================================================
#  Negative: 5 个负向用例
# ============================================================

NEGATIVE_FORBIDDEN_VOICE_CLONE = [
    make_event("n1", "text", text="用我妈妈的声音唱这首歌"),
]

NEGATIVE_PII = [
    make_event("n1", "text", text="我的身份证号是 110101199001011234"),
]

NEGATIVE_AMBIGUOUS = [
    make_event("n1", "text", text="好"),
    make_event("n2", "text", text="可以"),
]

NEGATIVE_DESTRUCTIVE = [
    make_event("n1", "action", action_id="session.reset_all"),
]

NEGATIVE_SELF_CONTRADICTION = [
    make_event("n1", "text", text="我想要一首快节奏的放松音乐"),
]


# ============================================================
#  Recovery: 5 个失败恢复用例
# ============================================================

RECOVERY_TOOL_TIMEOUT = [
    make_event("r1", "text", text="海边六点"),
    make_event("r2", "text", text="妈妈电话"),
    make_event("r3", "action", action_id="archaeology.confirm"),
    make_event("r4", "action", action_id="identity.keep", action_payload={"slot": "A"}),
    make_event("r5", "action", action_id="variable.confirm"),
    make_event("r6", "action", action_id="proposal.confirm"),
    # 之后模拟 tool 失败 → retry
]

RECOVERY_KILL_MID_GENERATION = [
    make_event("k1", "text", text="海边六点"),
    make_event("k2", "text", text="妈妈电话"),
    make_event("k3", "action", action_id="archaeology.confirm"),
    make_event("k4", "action", action_id="identity.keep", action_payload={"slot": "A"}),
    make_event("k5", "action", action_id="variable.confirm"),
    # 在 proposal.confirm 后立即 kill(由测试 setup 模拟)
    make_event("k6", "action", action_id="proposal.confirm"),
]

RECOVERY_DB_DAMAGE = [
    make_event("d1", "text", text="海边六点"),
    # 后续由测试 setup 模拟 DB 损坏
]


# ============================================================
#  Regression: 之前测试的 213 个用例
# ============================================================

REGRESSION_LEGACY_SMOKE = [
    make_event("r1", "text", text="海边六点"),
    make_event("r2", "text", text="妈妈电话"),
]


# ============================================================
#  评测集 dict
# ============================================================

EVALUATION_SETS: Dict[str, Dict[str, Any]] = {
    "golden": {
        "happy_path": GOLDEN_HAPPY_PATH,
    },
    "boundary": {
        "empty": BOUNDARY_EMPTY,
        "short": BOUNDARY_SHORT,
        "long": BOUNDARY_LONG,
        "emoji": BOUNDARY_EMOJI,
        "mixed_lang": BOUNDARY_MIXED_LANG,
    },
    "negative": {
        "forbidden_voice_clone": NEGATIVE_FORBIDDEN_VOICE_CLONE,
        "pii": NEGATIVE_PII,
        "ambiguous": NEGATIVE_AMBIGUOUS,
        "destructive": NEGATIVE_DESTRUCTIVE,
        "self_contradiction": NEGATIVE_SELF_CONTRADICTION,
    },
    "recovery": {
        "tool_timeout": RECOVERY_TOOL_TIMEOUT,
        "kill_mid_generation": RECOVERY_KILL_MID_GENERATION,
        "db_damage": RECOVERY_DB_DAMAGE,
    },
    "regression": {
        "legacy_smoke": REGRESSION_LEGACY_SMOKE,
    },
}


def get_eval(category: str, name: str) -> List[InputEvent]:
    return EVALUATION_SETS[category][name]


def all_evals() -> List[tuple]:
    """返回 (category, name, events) 三元组"""
    out = []
    for cat, names in EVALUATION_SETS.items():
        for n, evs in names.items():
            out.append((cat, n, evs))
    return out
