"""
Intent Recognition — 把用户文本解析为意图

M1 stub 是 regex + 关键词,真实 LLM 时只换这一处。

支持的 intent:
  - bpm_change     "BPM 88" / "改成 100 BPM" / "节奏快一点"
  - key_change     "降 B 大调" / "C minor" / "换成 D 调"
  - genre_change   "换成电子" / "改成爵士" / "加一点 R&B"
  - lyric_edit     "第 3 句改一下" / "副歌改" / "主歌加一句"
  - instrument_add "加钢琴" / "加吉他"
  - skip_stage     "跳过" / "下一步" / "继续"
  - back_stage     "上一步" / "返回" / "退回去"
  - revise_request "改副歌" / "重写" / "再来一次"
  - approve_review "整体满意" / "可以" / "通过"
  - confirm_yes    "确认" / "是" / "OK 改"
  - reject_cancel  "取消" / "不" / "算了"

每个 intent 返回:
  - detected (bool)
  - parameters (dict)
  - confidence (0-1)
"""
import re
from dataclasses import dataclass, field
from typing import Optional


@dataclass
class Intent:
    name: str
    parameters: dict = field(default_factory=dict)
    confidence: float = 1.0
    raw_match: str = ""


# 中文 / 英文 BPM 模式
BPM_PATTERNS = [
    re.compile(r'\b(\d{2,3})\s*(?:bpm|BPM)', re.IGNORECASE),
    re.compile(r'(?:bpm|BPM)\s*(\d{2,3})', re.IGNORECASE),
    re.compile(r'(?:改成|设为|变成|调到|弄成|设为)?\s*(\d{2,3})\s*(?:bpm|BPM|拍)', re.IGNORECASE),
    re.compile(r'(?:节奏|速度|拍速)\s*(\d{2,3})'),
    re.compile(r'快\s*一点|慢\s*一点|更\s*快|更\s*慢'),
]

# 调性 — 必须带"调"或升降号,避免误识别单词里的字母
KEY_PATTERNS = [
    re.compile(r'([A-Ga-g])([#♯b♭]?)\s*(?:maj|min|major|minor)', re.IGNORECASE),
    re.compile(r'(降|升|不降不升)\s*([A-Ga-g])(?:\s*(?:大调|小调|maj|min))?', re.IGNORECASE),
    re.compile(r'([A-Ga-g])\s*(?:大调|小调)', re.IGNORECASE),
    re.compile(r'换成?\s*([A-Ga-g])\s*调'),
    re.compile(r'(?<![A-Za-z])([A-G])\s*调'),  # 单独的 "[A-G] 调" 不在单词中
]

# 风格 / 类型
GENRE_KEYWORDS = {
    "电子": ["电子", "electronic", "edm", "techno", "house", "synth"],
    "爵士": ["爵士", "jazz", "swing"],
    "民谣": ["民谣", "folk", "acoustic"],
    "嘻哈": ["嘻哈", "hip-hop", "rap", "trap"],
    "摇滚": ["摇滚", "rock", "grunge", "indie"],
    "R&B": ["r&b", "rnb", "soul", "灵魂"],
    "古典": ["古典", "classical", "orchestral", "管弦"],
    "城市流行": ["流行", "city pop", "pop", "city"],
    "氛围": ["氛围", "ambient", "atmospheric", "梦幻"],
}

# 乐器
INSTRUMENT_KEYWORDS = {
    "钢琴": ["钢琴", "piano"],
    "吉他": ["吉他", "guitar"],
    "贝斯": ["贝斯", "bass"],
    "鼓": ["鼓", "drums", "drum"],
    "弦乐": ["弦乐", "strings", "orchestral"],
    "合成器": ["合成器", "synthesizer", "synth"],
    "萨克斯": ["萨克斯", "saxophone", "sax"],
}

# 歌词编辑
LYRIC_EDIT_PATTERNS = [
    re.compile(r'(?:第)?\s*(\d+)\s*句\s*改', re.IGNORECASE),
    re.compile(r'(主歌|副歌|桥段|bridge|verse|chorus|pre-chorus|intro|outro)', re.IGNORECASE),
    re.compile(r'歌词.*?改|改.*?歌词'),
]

# 阶段控制
SKIP_KEYWORDS = ["跳过", "下一步", "继续", "skip", "next", "走起", "go"]
BACK_KEYWORDS = ["上一步", "返回", "退回去", "back", "上阶段", "回退"]


def detect_intent(text: str) -> Optional[Intent]:
    """识别文本意图

    优先级(冲突时):
      1. stage 控制(skip/back)
      2. BPM / Key 数字类(精确)
      3. Genre / Instrument
      4. Lyric edit
      5. Revise / Approve
      6. Confirm / Cancel
    """
    if not text or not text.strip():
        return None

    text_clean = text.strip()

    # 1. skip stage
    for kw in SKIP_KEYWORDS:
        if kw.lower() in text_clean.lower():
            return Intent(name="skip_stage", parameters={}, confidence=0.9,
                          raw_match=text_clean)

    # 2. back stage
    for kw in BACK_KEYWORDS:
        if kw.lower() in text_clean.lower():
            return Intent(name="back_stage", parameters={}, confidence=0.9,
                          raw_match=text_clean)

    # 3. BPM — 模式 0,1,2,3 都是绝对值
    for pat in BPM_PATTERNS[:4]:
        m = pat.search(text_clean)
        if m and m.groups() and m.group(1) and m.group(1).isdigit():
            bpm = int(m.group(1))
            if 30 <= bpm <= 240:
                return Intent(
                    name="bpm_change",
                    parameters={"bpm": bpm, "absolute": True},
                    confidence=0.95,
                    raw_match=m.group(0),
                )

    # 相对 BPM 模式
    if BPM_PATTERNS[4].search(text_clean):
        direction = "up" if ("快" in text_clean or "加速" in text_clean) else "down"
        return Intent(
            name="bpm_change",
            parameters={"bpm": "relative", "direction": direction},
            confidence=0.7,
            raw_match=text_clean,
        )

    # 4. Key
    for pat in KEY_PATTERNS:
        m = pat.search(text_clean)
        if m:
            return Intent(
                name="key_change",
                parameters={"raw": m.group(0)},
                confidence=0.85,
                raw_match=m.group(0),
            )

    # 5. Genre
    text_lower = text_clean.lower()
    for genre, kws in GENRE_KEYWORDS.items():
        for kw in kws:
            if kw.lower() in text_lower:
                return Intent(
                    name="genre_change",
                    parameters={"genre": genre, "raw": kw},
                    confidence=0.85,
                    raw_match=kw,
                )

    # 6. Instrument add
    for instr, kws in INSTRUMENT_KEYWORDS.items():
        for kw in kws:
            if kw in text_clean or kw.lower() in text_lower:
                return Intent(
                    name="instrument_add",
                    parameters={"instrument": instr, "raw": kw},
                    confidence=0.8,
                    raw_match=kw,
                )

    # 7. Lyric edit
    for pat in LYRIC_EDIT_PATTERNS:
        m = pat.search(text_clean)
        if m:
            return Intent(
                name="lyric_edit",
                parameters={"raw": m.group(0), "target": m.group(1) if m.groups() else m.group(0)},
                confidence=0.85,
                raw_match=m.group(0),
            )

    # 8. Revise request — STRICT imperative patterns only
    #
    # Background (2026-09-18 bug fix):
    #   旧实现用 substring 匹配 ["改", "重新", "再来", ...],会把叙述语境误判成修订请求。
    #   例子:"长大后**重新**回到小时候住过的海边" → 错配 revise_request
    #   新规则:只匹配 imperative 形式(句首动词 / "改成 X" / 短命令)。
    #
    # 触发条件(任一):
    #   A. 句首是 revise 动词:改 / 改成 / 改一下 / 修改 / 调整 / 调成 / 重写 / 重做 / 再来 / 重新写 / 重新做
    #   B. 短命令(≤8 字)且包含 revise 动词:重写 / 改一下 / 再来一次
    #   C. "改成/重写/重做/重写一下/调成" + 后接目标
    REVISE_START_RE = re.compile(
        r'^(?:改|改成|改一下|修改|调整|调成|调一下|重写|重写一下|重做|重做一下|再来|再来一次|再来一遍|重新写|重新做|重新唱|重新来|重新开始)'
    )
    REVISE_WITH_TARGET_RE = re.compile(
        r'(?:改成|重写|重写一下|重做|重做一下|再来一次|调成)\s*[^\s,。,!！?？、;；]+'
    )
    if REVISE_START_RE.search(text_clean):
        return Intent(name="revise_request", parameters={"raw": text_clean},
                      confidence=0.85, raw_match=text_clean)
    if len(text_clean) <= 8 and any(
        kw in text_clean for kw in ["重写", "改一下", "再来", "重做", "修改", "调整"]
    ):
        return Intent(name="revise_request", parameters={"raw": text_clean},
                      confidence=0.85, raw_match=text_clean)
    m_target = REVISE_WITH_TARGET_RE.search(text_clean)
    if m_target:
        return Intent(name="revise_request", parameters={"raw": text_clean},
                      confidence=0.8, raw_match=m_target.group(0))

    # 9. Approve
    if any(kw in text_clean for kw in ["满意", "可以", "通过", "approve", "good", "ok 了"]):
        return Intent(name="approve_review", parameters={}, confidence=0.85,
                      raw_match=text_clean)

    # 10. Confirm yes
    if text_clean.strip().lower() in ("确认", "是", "yes", "y", "好", "对"):
        return Intent(name="confirm_yes", parameters={}, confidence=0.9,
                      raw_match=text_clean)

    # 11. Cancel
    if text_clean.strip().lower() in ("取消", "不", "no", "n", "算了", "不做了"):
        return Intent(name="reject_cancel", parameters={}, confidence=0.9,
                      raw_match=text_clean)

    return None


def intent_to_action(intent: Intent, current_revision: Optional[int] = None,
                     current_music_spec: Optional[dict] = None) -> Optional[dict]:
    """把 intent 转成 ActionEvent(action_id + payload + state_mutation)

    不一定每个 intent 都有对应 action_id — 返回 None 表示"no action"

    Args:
        intent: 识别出的意图
        current_revision: 当前 ledger revision(用于乐观锁)
        current_music_spec: 当前 music_spec(用于 variable 写入)
    """
    spec = current_music_spec or {}
    cur_rev = current_revision if current_revision is not None else 0

    def _make_music_spec_mutation(key: str, value):
        """构造 set 一个 music_spec 字段的 state_mutation"""
        return {
            "expected_revision": cur_rev,
            "operations": [
                {"op": "set", "path": f"/music_spec/{key}", "value": value},
            ],
        }

    mapping = {
        "bpm_change": {
            "action_id": "variable.change",
            "payload_builder": lambda p: {
                "name": "bpm",
                "value": p["bpm"],
                "intent": "bpm_change",
                "state_mutation": _make_music_spec_mutation("bpm", p["bpm"]),
            }
        },
        "key_change": {
            "action_id": "variable.change",
            "payload_builder": lambda p: {
                "name": "key",
                "value": p["raw"],
                "intent": "key_change",
                "state_mutation": _make_music_spec_mutation("key", p["raw"]),
            }
        },
        "genre_change": {
            "action_id": "variable.change",
            "payload_builder": lambda p: {
                "name": "genre",
                "value": p["genre"],
                "intent": "genre_change",
                "state_mutation": _make_music_spec_mutation("genre", p["genre"]),
            }
        },
        "instrument_add": {
            "action_id": "variable.change",
            "payload_builder": lambda p: {
                "name": "instrument",
                "value": p["instrument"],
                "intent": "instrument_add",
                "state_mutation": _make_music_spec_mutation("instrument", p["instrument"]),
            }
        },
        "lyric_edit": {
            "action_id": "proposal.edit",
            "payload_builder": lambda p: {
                "item": f"lyrics.{p['target']}",
                "raw": p["raw"],
                "intent": "lyric_edit",
                "state_mutation": {
                    "expected_revision": cur_rev,
                    "operations": [
                        {"op": "append", "path": "/proposal_edits",
                         "item": {"target": p["target"], "raw": p["raw"]}},
                    ],
                },
            }
        },
        "skip_stage": {
            "action_id": "stage.skip",
            "payload_builder": lambda p: {}
        },
        "back_stage": {
            "action_id": "stage.back",
            "payload_builder": lambda p: {}
        },
        "revise_request": {
            "action_id": "revision.request",
            "payload_builder": lambda p: {"feedback": p["raw"]}
        },
        "approve_review": {
            "action_id": "review.approve",
            "payload_builder": lambda p: {}
        },
    }
    mapping_entry = mapping.get(intent.name)
    if not mapping_entry:
        return None
    return {
        "action_id": mapping_entry["action_id"],
        "payload": mapping_entry["payload_builder"](intent.parameters),
    }
